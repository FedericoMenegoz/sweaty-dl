#!/usr/bin/env bash
# Called by deployment.yml on Ubuntu; authentication uses only GITHUB_TOKEN.
set -euo pipefail

release_state() {
  # List requests must succeed. An API/authentication failure is never treated
  # as a missing release. Pagination also covers old releases on retries.
  gh api --paginate "repos/$GITHUB_REPOSITORY/releases?per_page=100" \
    --jq '.[] | {tag_name, draft}' |
    jq -sr --arg tag "$TAG" '.[] | select(.tag_name == $tag) | if .draft then "draft" else "published" end'
}

verify_assets() {
  env -u GH_TOKEN -u GITHUB_TOKEN -u TAURI_SIGNING_PRIVATE_KEY -u TAURI_SIGNING_PRIVATE_KEY_PASSWORD \
    python3 -B scripts/updater_manifest.py --tag "$TAG" --repository "$GITHUB_REPOSITORY" \
    --source deployments --commit "$GITHUB_SHA" --verify-signatures
}

case "${1:-}" in
  prepare)
    TAG="$(python3 -B scripts/release_version.py next "$GITHUB_SHA")"
    state="$(release_state)"
    echo "tag=$TAG" >> "$GITHUB_OUTPUT"
    echo "published=$([[ "$state" == published ]] && echo true || echo false)" >> "$GITHUB_OUTPUT"
    echo "Release $TAG from $GITHUB_SHA (${state:-new})"
    ;;
  verify)
    verify_assets
    ;;
  publish)
    [[ "$TAG" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
    # Fetch again: even on a delayed retry, never move an existing tag or
    # attach artifacts to a different source commit.
    git fetch origin 'refs/tags/*:refs/tags/*'
    if git show-ref --verify --quiet "refs/tags/$TAG"; then
      if [[ "$(git rev-parse "$TAG^{commit}")" != "$GITHUB_SHA" ]]; then
        echo "::error::$TAG belongs to another commit. Re-run all jobs to recalculate the version."
        exit 1
      fi
    fi
    state="$(release_state)"
    if [[ "$state" == published ]]; then
      echo "$TAG is already published; nothing to do."
      exit 0
    fi

    verify_assets
    # Retrying an older draft must not replace a newer published update feed.
    published_tags="$(gh api --paginate "repos/$GITHUB_REPOSITORY/releases?per_page=100" \
      --jq '.[] | select(.draft == false and .prerelease == false) | .tag_name')"
    highest_published="$(printf '%s\n' "$published_tags" | python3 -B scripts/release_version.py highest)"
    highest="$(printf '%s\n%s\n' "$published_tags" "$TAG" | python3 -B scripts/release_version.py highest)"
    latest=false
    if [[ "$highest" == "$TAG" ]]; then
      latest=true
      if [[ -n "$highest_published" ]]; then
        # A queued run may be based on an older or divergent source commit.
        # Never let such a run publish a newer update feed than the source of
        # the highest already-published stable release.
        if ! git show-ref --verify --quiet "refs/tags/$highest_published"; then
          echo "::error::Published release $highest_published has no matching Git tag; refusing to publish $TAG as latest."
          exit 1
        fi
        published_sha="$(git rev-parse "$highest_published^{commit}")"
        if ! git merge-base --is-ancestor "$published_sha" "$GITHUB_SHA"; then
          echo "::error::$TAG would become latest from a source that does not descend from published $highest_published. Re-run from a commit based on the latest deployment source."
          exit 1
        fi
      fi
    fi
    if ! git show-ref --verify --quiet "refs/tags/$TAG"; then
      gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs" \
        -f "ref=refs/tags/$TAG" -f "sha=$GITHUB_SHA" > /dev/null
      git tag "$TAG" "$GITHUB_SHA"
    fi
    if [[ -z "$state" ]]; then
      gh release create "$TAG" --verify-tag --target "$GITHUB_SHA" --draft --title "$TAG" \
        --notes "Source commit: $GITHUB_SHA
Build: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID
Each platform's build-result.json records package sizes and SHA-256 checksums."
    fi
    # A failed upload leaves a draft; a retry replaces partial assets, then
    # publishes only after every upload succeeds. Published assets are untouched.
    gh release upload "$TAG" release-assets/* --clobber
    gh release edit "$TAG" --draft=false --latest="$latest"
    ;;
  *) echo 'Usage: bash scripts/release.sh prepare|verify|publish' >&2; exit 1 ;;
esac
