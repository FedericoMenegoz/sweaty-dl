; The 0.1.1 NSIS installer stored its location under the old product name.
; During an in-app update, reuse that location and register the renamed product.
Var SweatyDLLegacyInstallDir

!macro NSIS_HOOK_PREINSTALL
  StrCpy $SweatyDLLegacyInstallDir ""
  ${If} $UpdateMode = 1
    ReadRegStr $0 SHCTX "${MANUPRODUCTKEY}" ""
    ${If} $0 == ""
      ReadRegStr $0 SHCTX "${MANUKEY}\SweatyDL Next" ""
      ReadRegStr $1 SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\SweatyDL Next" "MainBinaryName"
      ${If} $0 != ""
      ${AndIf} $1 == "${MAINBINARYNAME}.exe"
      ${AndIf} ${FileExists} "$0\${MAINBINARYNAME}.exe"
        ; Preserve the existing installer's downgrade check for the legacy key.
        !if "${ALLOWDOWNGRADES}" == "false"
          ReadRegStr $2 SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\SweatyDL Next" "DisplayVersion"
          nsis_tauri_utils::SemverCompare "${VERSION}" $2
          Pop $2
          ${If} $2 = -1
            Abort "A newer SweatyDL installation is already present."
          ${EndIf}
        !endif
        StrCpy $SweatyDLLegacyInstallDir $0
        StrCpy $INSTDIR $0
        ; Tauri sets the output path before invoking this hook.
        SetOutPath $INSTDIR
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ${If} $SweatyDLLegacyInstallDir != ""
  ${AndIf} $SweatyDLLegacyInstallDir == $INSTDIR
    ReadRegStr $0 SHCTX "${UNINSTKEY}" "DisplayVersion"
    ${If} $0 == "${VERSION}"
      ; Remove only the superseded installer records after the new ones exist.
      DeleteRegKey SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\SweatyDL Next"
      DeleteRegKey SHCTX "${MANUKEY}\SweatyDL Next"
      ; Rename only shortcuts that target this installation, without replacing
      ; an existing destination shortcut or touching application data.
      !insertmacro IsShortcutTarget "$SMPROGRAMS\SweatyDL Next.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
      Pop $0
      ${If} $0 = 1
      ${AndIfNot} ${FileExists} "$SMPROGRAMS\${PRODUCTNAME}.lnk"
        Rename "$SMPROGRAMS\SweatyDL Next.lnk" "$SMPROGRAMS\${PRODUCTNAME}.lnk"
      ${EndIf}
      !insertmacro IsShortcutTarget "$DESKTOP\SweatyDL Next.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
      Pop $0
      ${If} $0 = 1
      ${AndIfNot} ${FileExists} "$DESKTOP\${PRODUCTNAME}.lnk"
        Rename "$DESKTOP\SweatyDL Next.lnk" "$DESKTOP\${PRODUCTNAME}.lnk"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend
