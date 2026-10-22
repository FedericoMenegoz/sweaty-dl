use serde_json::{json, Value};
use tauri::Url;

pub const MAX_SNAPSHOT_BYTES: usize = 512 * 1024;

// Read only catalog metadata from the isolated Bandcamp page.
// This script neither automates the challenge nor gives the page an IPC channel.
pub const READ_RESULTS: &str = include_str!("bandcamp_snapshot.js");

pub fn authorized_local(label: &str, url: &Url, controls: bool) -> bool {
    let label_allowed = label == "main" || (controls && label == "bandcamp-controls");
    let origin_allowed = (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))
        || (cfg!(debug_assertions)
            && url.scheme() == "http"
            && url.host_str() == Some("127.0.0.1")
            && url.port() == Some(1420));
    label_allowed && origin_allowed
}

pub fn allowed_navigation(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("bandcamp.com")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
}

pub fn search_url(query: &str) -> Result<Url, String> {
    if query.trim().is_empty() || query.chars().count() > 1001 {
        return Err("Ricerca Bandcamp non valida.".into());
    }
    let mut url = Url::parse("https://bandcamp.com/search").unwrap();
    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("item_type", "t");
    Ok(url)
}

pub fn is_search_page(url: &Url) -> bool {
    let pairs = url.query_pairs().collect::<Vec<_>>();
    let queries = pairs
        .iter()
        .filter(|(key, _)| key == "q")
        .collect::<Vec<_>>();
    let types = pairs
        .iter()
        .filter(|(key, _)| key == "item_type")
        .collect::<Vec<_>>();
    allowed_navigation(url)
        && ["/search", "/search/"].contains(&url.path())
        && url.as_str().len() <= 8192
        && queries.len() == 1
        && !queries[0].1.trim().is_empty()
        && queries[0].1.chars().count() <= 1001
        && types.len() <= 1
}

pub fn matches_search(url: &Url, expected: &Url) -> bool {
    let mut actual = url.query_pairs().collect::<Vec<_>>();
    let mut source = expected.query_pairs().collect::<Vec<_>>();
    actual.sort();
    source.sort();
    is_search_page(url) && is_search_page(expected) && actual == source
}

pub fn catalog_url(value: &str) -> Result<Url, String> {
    let mut url =
        Url::parse(value).map_err(|_| "URL risultato Bandcamp non valido.".to_string())?;
    let host = url.host_str().unwrap_or("");
    if value.len() > 8192
        || url.scheme() != "https"
        || !(host == "bandcamp.com" || host.ends_with(".bandcamp.com"))
        || url.port().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || !["/track/", "/album/"]
            .iter()
            .any(|prefix| url.path().starts_with(prefix) && url.path().len() > prefix.len())
    {
        return Err("La pagina contiene un collegamento Bandcamp non valido.".into());
    }
    url.set_query(None);
    url.set_fragment(None);
    Ok(url)
}

pub fn selected_report(report: &Value, selected: &[String]) -> Result<Value, String> {
    if selected.is_empty() || selected.len() > 100 {
        return Err("Seleziona almeno un risultato da confermare.".into());
    }
    let rows = report["results"]
        .as_array()
        .ok_or("Leggi prima i risultati Bandcamp.")?;
    let mut results = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for url in selected {
        let row = rows
            .iter()
            .find(|row| row["url"].as_str() == Some(url))
            .ok_or("La selezione non appartiene ai risultati letti. Aggiorna i risultati.")?;
        if seen.insert(url) {
            results.push(row.clone());
        }
    }
    Ok(json!({"results": results, "errors": {}, "searched": ["Bandcamp"], "browser_query": null}))
}

fn validated_selection_hint(snapshot: &Value, results: &[Value]) -> Option<Value> {
    let hint = snapshot.get("selection_hint")?;
    let link = catalog_url(hint.get("link_url")?.as_str()?).ok()?;
    let result = catalog_url(hint.get("result_url")?.as_str()?).ok()?;
    if !results
        .iter()
        .any(|row| row["url"].as_str() == Some(result.as_str()))
    {
        return None;
    }
    // A cover may link to the album containing this result. Never associate
    // unrelated artists/domains or downgrade a different track into this one.
    if link != result
        && !(link.host_str() == result.host_str() && link.path().starts_with("/album/"))
    {
        return None;
    }
    Some(json!({"link_url": link.as_str(), "result_url": result.as_str()}))
}

fn text<'a>(value: &'a Value, key: &str, max: usize) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| s.chars().count() <= max)
        .ok_or_else(|| "Risultati Bandcamp non validi o troppo grandi.".into())
}

pub fn validate_snapshot(raw: &str, expected: &Url) -> Result<Value, String> {
    if raw.len() > MAX_SNAPSHOT_BYTES {
        return Err("La pagina Bandcamp contiene troppi dati da importare.".into());
    }
    let mut snapshot: Value = serde_json::from_str(raw)
        .map_err(|_| "Impossibile leggere la pagina Bandcamp.".to_string())?;
    // A string result is portable across WebKit/WebView2; also accept the
    // previous direct-object callback representation.
    if let Some(serialized) = snapshot.as_str() {
        snapshot = serde_json::from_str(serialized)
            .map_err(|_| "Risposta Bandcamp non leggibile.".to_string())?;
    }
    if snapshot.get("error").is_some() {
        return Err(
            "Lettura della pagina Bandcamp non riuscita. Aggiorna i risultati e riprova.".into(),
        );
    }
    let url = Url::parse(text(&snapshot, "url", 8192)?)
        .map_err(|_| "URL Bandcamp non valido.".to_string())?;
    if !matches_search(&url, expected) {
        return Err(
            "La pagina Bandcamp è cambiata durante la lettura. Riprova da quella visualizzata."
                .into(),
        );
    }
    if snapshot.get("challenge").and_then(Value::as_bool) == Some(true) {
        return Err(
            "La verifica Bandcamp è ancora presente. Completala nella finestra dedicata.".into(),
        );
    }
    if snapshot.get("recognized").and_then(Value::as_bool) != Some(true) {
        return Err("Pagina Bandcamp non riconosciuta: nessun risultato importato.".into());
    }
    let rows = snapshot
        .get("results")
        .and_then(Value::as_array)
        .filter(|rows| rows.len() <= 100)
        .ok_or_else(|| "Troppi risultati Bandcamp o risposta non valida.".to_string())?;
    let mut results = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for row in rows {
        let title = text(row, "title", 500)?.trim();
        let artist = text(row, "artist", 500)?.trim();
        if title.is_empty() {
            return Err("Un risultato Bandcamp non ha un titolo valido.".into());
        }
        let track_url = catalog_url(text(row, "url", 4096)?)?;
        if seen.insert(track_url.to_string()) {
            results.push(json!({
                "title": title, "artist": artist, "url": track_url.as_str(),
                "source": "Bandcamp",
                "kind": if track_url.path().starts_with("/album/") { "album" } else { "track" },
                "duration": null, "artwork": "",
                "capabilities": { "catalog": true, "preview": false, "download": false }
            }));
        }
    }
    let selection_hint = validated_selection_hint(&snapshot, &results);
    // A container alone can be a loading skeleton. Only finish an empty search
    // when the loaded page explicitly identifies it as empty.
    let ready_for_import = snapshot["ready_state"].as_str() == Some("complete")
        && (!results.is_empty() || snapshot["empty_confirmed"].as_bool() == Some(true));
    Ok(
        json!({"results": results, "errors": {}, "searched": ["Bandcamp"], "browser_query": null,
        "selection_hint": selection_hint, "ready_for_import": ready_for_import}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Value {
        json!({"url": search_url("El Sonido").unwrap().as_str(), "recognized": true,
            "challenge": false, "results": [{"title":"El Sonido", "artist":"Truncate",
            "url":"https://truncate.bandcamp.com/track/el-sonido?from=search"}]})
    }

    #[test]
    fn imports_catalog_only_and_deduplicates() {
        let mut page = snapshot();
        let duplicate = page["results"][0].clone();
        page["results"].as_array_mut().unwrap().push(duplicate);
        let report =
            validate_snapshot(&page.to_string(), &search_url("El Sonido").unwrap()).unwrap();
        assert_eq!(report["results"].as_array().unwrap().len(), 1);
        assert_eq!(
            report["results"][0]["url"],
            "https://truncate.bandcamp.com/track/el-sonido"
        );
        assert_eq!(report["results"][0]["capabilities"]["download"], false);
    }

    #[test]
    fn rejects_other_pages_and_queries() {
        let expected = search_url("El Sonido").unwrap();
        for url in [
            "https://bandcamp.com.evil.test/search?q=El+Sonido&item_type=t",
            "http://bandcamp.com/search?q=El+Sonido&item_type=t",
            "https://bandcamp.com/search?q=Another&item_type=t",
            "https://bandcamp.com/album/test",
        ] {
            let mut page = snapshot();
            page["url"] = json!(url);
            assert!(validate_snapshot(&page.to_string(), &expected).is_err());
        }
        let reordered =
            Url::parse("https://bandcamp.com/search?item_type=t&q=El%20Sonido").unwrap();
        assert!(matches_search(&reordered, &expected));
        let duplicate =
            Url::parse("https://bandcamp.com/search?q=El+Sonido&q=Another&item_type=t").unwrap();
        assert!(!matches_search(&duplicate, &expected));
        let redirected =
            Url::parse("https://bandcamp.com/search?q=El+Sonido&from=search&page=2").unwrap();
        assert!(is_search_page(&redirected));
        assert!(!matches_search(&redirected, &expected));
        let changed_type =
            Url::parse("https://bandcamp.com/search?q=El+Sonido&item_type=a").unwrap();
        assert!(!matches_search(&changed_type, &expected));
        assert!(is_search_page(&changed_type));
        assert!(matches_search(&changed_type, &changed_type));
    }

    #[test]
    fn rejects_unsafe_result_urls_and_oversize_data() {
        let expected = search_url("El Sonido").unwrap();
        for url in [
            "javascript:alert(1)",
            "https://artist.bandcamp.com.evil.test/track/test",
            "https://user:pass@artist.bandcamp.com/track/test",
            "https://artist.bandcamp.com/",
        ] {
            let mut page = snapshot();
            page["results"][0]["url"] = json!(url);
            assert!(validate_snapshot(&page.to_string(), &expected).is_err());
        }
        assert!(validate_snapshot(&" ".repeat(MAX_SNAPSHOT_BYTES + 1), &expected).is_err());
        let mut page = snapshot();
        page["results"][0]["title"] = json!("x".repeat(501));
        assert!(validate_snapshot(&page.to_string(), &expected).is_err());
    }

    #[test]
    fn distinguishes_challenge_unknown_and_empty() {
        let expected = search_url("El Sonido").unwrap();
        let mut page = snapshot();
        page["challenge"] = json!(true);
        assert!(validate_snapshot(&page.to_string(), &expected)
            .unwrap_err()
            .contains("verifica"));
        page["challenge"] = json!(false);
        page["recognized"] = json!(false);
        assert!(validate_snapshot(&page.to_string(), &expected).is_err());
        page["recognized"] = json!(true);
        page["results"] = json!([]);
        assert!(
            validate_snapshot(&page.to_string(), &expected).unwrap()["results"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn accepts_string_callback_and_rejects_failed_collector() {
        let expected = search_url("El Sonido").unwrap();
        let serialized = serde_json::to_string(&snapshot().to_string()).unwrap();
        assert_eq!(
            validate_snapshot(&serialized, &expected).unwrap()["results"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        for raw in [
            "null",
            "{}",
            "{\"error\":\"dom_read_failed\"}",
            "\"invalid json\"",
        ] {
            assert!(validate_snapshot(raw, &expected).is_err());
        }
    }

    #[test]
    fn confirms_only_selected_validated_catalog_rows() {
        let report =
            validate_snapshot(&snapshot().to_string(), &search_url("El Sonido").unwrap()).unwrap();
        let url = "https://truncate.bandcamp.com/track/el-sonido".to_string();
        let selected = selected_report(&report, &[url.clone(), url]).unwrap();
        assert_eq!(selected["results"].as_array().unwrap().len(), 1);
        assert_eq!(selected["results"][0]["capabilities"]["download"], false);
        assert!(selected_report(&report, &[]).is_err());
        assert!(selected_report(
            &report,
            &["https://unknown.bandcamp.com/track/other".into()]
        )
        .is_err());
    }

    #[test]
    fn remote_page_never_gets_control_access() {
        let local = Url::parse("http://127.0.0.1:1420/index.html?view=bandcamp").unwrap();
        let remote = search_url("El Sonido").unwrap();
        assert!(authorized_local("bandcamp-controls", &local, true));
        assert!(!authorized_local("bandcamp-controls", &local, false));
        assert!(!authorized_local("bandcamp-page", &local, true));
        assert!(!authorized_local("bandcamp-controls", &remote, true));
        assert!(!authorized_local("main", &remote, true));
    }

    #[test]
    fn click_hint_must_refer_to_a_validated_row_on_the_same_artist_host() {
        let expected = search_url("El Sonido").unwrap();
        let mut page = snapshot();
        page["selection_hint"] = json!({
            "link_url": "https://truncate.bandcamp.com/album/repeat?from=search",
            "result_url": "https://truncate.bandcamp.com/track/el-sonido?from=search"
        });
        let report = validate_snapshot(&page.to_string(), &expected).unwrap();
        assert_eq!(
            report["selection_hint"]["result_url"],
            "https://truncate.bandcamp.com/track/el-sonido"
        );
        for (key, url) in [
            (
                "result_url",
                "https://truncate.bandcamp.com/track/el-sonido-live",
            ),
            (
                "link_url",
                "https://truncate.bandcamp.com/track/el-sonido-remix",
            ),
            ("link_url", "https://another.bandcamp.com/album/repeat"),
            ("link_url", "javascript:alert(1)"),
        ] {
            let mut invalid = page.clone();
            invalid["selection_hint"][key] = json!(url);
            assert!(
                validate_snapshot(&invalid.to_string(), &expected).unwrap()["selection_hint"]
                    .is_null()
            );
        }
    }

    #[test]
    fn imports_the_visible_search_including_refined_query_filters_and_pagination() {
        let launch = search_url("El Sonido").unwrap();
        let visible = Url::parse(
            "https://bandcamp.com/search/?q=El+Sonido+Live&item_type=a&page=2&from=search",
        )
        .unwrap();
        assert!(is_search_page(&visible));
        assert!(!matches_search(&visible, &launch));
        let mut page = snapshot();
        page["url"] = json!(visible.as_str());
        assert!(validate_snapshot(&page.to_string(), &visible).is_ok());
        assert!(validate_snapshot(&page.to_string(), &launch).is_err());
        for value in [
            "https://bandcamp.com/search",
            "https://bandcamp.com/search?q=",
            "https://bandcamp.com/search?q=Song&q=Other",
            "https://bandcamp.com.evil.test/search?q=Song",
        ] {
            assert!(!is_search_page(&Url::parse(value).unwrap()));
        }
    }

    #[test]
    fn automatic_readiness_requires_a_loaded_catalog_or_explicit_empty_page() {
        let expected = search_url("El Sonido").unwrap();
        let mut page = snapshot();
        page["ready_state"] = json!("loading");
        assert_eq!(
            validate_snapshot(&page.to_string(), &expected).unwrap()["ready_for_import"],
            false
        );
        page["ready_state"] = json!("complete");
        assert_eq!(
            validate_snapshot(&page.to_string(), &expected).unwrap()["ready_for_import"],
            true
        );
        page["results"] = json!([]);
        assert_eq!(
            validate_snapshot(&page.to_string(), &expected).unwrap()["ready_for_import"],
            false
        );
        page["empty_confirmed"] = json!(true);
        assert_eq!(
            validate_snapshot(&page.to_string(), &expected).unwrap()["ready_for_import"],
            true
        );
        page["challenge"] = json!(true);
        assert!(validate_snapshot(&page.to_string(), &expected).is_err());
    }
}
