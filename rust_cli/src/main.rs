/// smm-fast-write — hot-path decision writer for smm-sync.
///
/// Reads JSON from stdin, validates required fields, appends one line to
/// .smm/decisions.jsonl and one audit line to .smm/compliance_lineage.jsonl,
/// then exits.  No network calls, no database connections, no embedding models.
///
/// Expected stdin JSON fields:
///   title      (required)  Short decision title
///   rationale  (required)  Why this decision was made
///   type       (required)  "architectural" | "technical" | "product"
///   confidence (optional)  Float 0-1, default 0.80
///   alternatives (optional) String or array of strings
///   constraints  (optional) String or array of strings
///   project    (optional)  Project name, default "smm-sync"
///   source     (optional)  Source type, default "manual"
///   made_by    (optional)  Who made the decision, default "lore-hook"
///
/// Output JSONL line format:
///   {"uuid":"...","title":"...","rationale":"...","type":"...","confidence":0.9,
///    "alternatives":"...","constraints":"...","timestamp":"2026-03-27T04:00:00Z",
///    "project":"...","source":"manual","made_by":"lore-hook"}
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use chrono::Utc;
use serde::Serialize;
use serde_json::Value;
use uuid::Uuid;

/// Normalize a raw type string to one of the 4 canonical decision types.
fn normalize_decision_type(t: &str) -> &'static str {
    match t.trim().to_lowercase().as_str() {
        "architectural" | "infrastructure" | "architecture" | "deployment" => "architectural",
        "product" | "feature" | "business" => "product",
        "constraint" | "limitation" => "constraint",
        // technical + all unrecognized types
        _ => "technical",
    }
}

// ---------------------------------------------------------------------------
// Output structs
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct DecisionRecord {
    uuid: String,
    title: String,
    rationale: String,
    #[serde(rename = "type")]
    decision_type: String,
    confidence: f64,
    alternatives: String,
    constraints: String,
    timestamp: String,
    project: String,
    source: String,
    made_by: String,
}

#[derive(Serialize)]
struct AuditEntry {
    entry_id: String,
    timestamp: String,
    event_type: String,
    decision_uuid: String,
    title: String,
    source: String,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Walk up from `cwd` until we find a directory containing `.smm/`.
fn find_smm_dir() -> Option<PathBuf> {
    let mut current = std::env::current_dir().ok()?;
    loop {
        let candidate = current.join(".smm");
        if candidate.is_dir() {
            return Some(candidate);
        }
        let parent = current.parent()?.to_path_buf();
        if parent == current {
            return None;
        }
        current = parent;
    }
}

/// Convert a JSON value (array or string) to a semicolon-joined string.
fn value_to_str(v: &Value) -> String {
    match v {
        Value::Array(arr) => arr
            .iter()
            .filter_map(|x| x.as_str())
            .collect::<Vec<_>>()
            .join("; "),
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// Append `line\n` to `path`, creating the file if absent.
/// Uses O_APPEND so concurrent writes from different processes are atomic
/// for writes < PIPE_BUF (4 KiB on Linux, 512 B on macOS — JSONL lines fit).
fn append_line(path: &Path, line: &str) -> io::Result<()> {
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    writeln!(file, "{}", line)
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn main() {
    // ── Read stdin ──────────────────────────────────────────────────────────
    let mut input = String::new();
    if let Err(e) = io::stdin().read_to_string(&mut input) {
        eprintln!("smm-fast-write: error reading stdin: {e}");
        std::process::exit(1);
    }

    // ── Parse JSON ──────────────────────────────────────────────────────────
    let parsed: Value = match serde_json::from_str(input.trim()) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("smm-fast-write: invalid JSON: {e}");
            std::process::exit(1);
        }
    };

    // ── Validate required fields ────────────────────────────────────────────
    let title = match parsed.get("title").and_then(|v| v.as_str()) {
        Some(t) if !t.trim().is_empty() => t.trim().to_string(),
        _ => {
            eprintln!("smm-fast-write: missing required field: title");
            std::process::exit(1);
        }
    };

    let rationale = match parsed.get("rationale").and_then(|v| v.as_str()) {
        Some(r) if !r.trim().is_empty() => r.trim().to_string(),
        _ => {
            eprintln!("smm-fast-write: missing required field: rationale");
            std::process::exit(1);
        }
    };

    // Accept "type" or "decision_type" (the Python CLI uses both)
    let decision_type_raw = parsed
        .get("type")
        .and_then(|v| v.as_str())
        .or_else(|| parsed.get("decision_type").and_then(|v| v.as_str()))
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "technical".to_string());
    let decision_type = normalize_decision_type(&decision_type_raw).to_string();

    // ── Optional fields with defaults ───────────────────────────────────────
    let confidence = parsed
        .get("confidence")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.80);

    let alternatives = parsed
        .get("alternatives")
        .map(|v| value_to_str(v))
        .unwrap_or_default();

    let constraints = parsed
        .get("constraints")
        .map(|v| value_to_str(v))
        .unwrap_or_default();

    let project = parsed
        .get("project")
        .and_then(|v| v.as_str())
        .unwrap_or("smm-sync")
        .to_string();

    let source = parsed
        .get("source")
        .and_then(|v| v.as_str())
        .unwrap_or("manual")
        .to_string();

    let made_by = parsed
        .get("made_by")
        .and_then(|v| v.as_str())
        .unwrap_or("lore-hook")
        .to_string();

    // ── Generate UUID + timestamp ────────────────────────────────────────────
    let decision_uuid = Uuid::new_v4().to_string();
    let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();

    // ── Locate .smm/ directory ───────────────────────────────────────────────
    let smm_dir = match find_smm_dir() {
        Some(d) => d,
        None => {
            eprintln!("smm-fast-write: could not find .smm/ directory. Run: smm init");
            std::process::exit(1);
        }
    };

    // ── Build + append decision record ──────────────────────────────────────
    let record = DecisionRecord {
        uuid: decision_uuid.clone(),
        title: title.clone(),
        rationale: rationale.clone(),
        decision_type,
        confidence,
        alternatives,
        constraints,
        timestamp: timestamp.clone(),
        project,
        source: source.clone(),
        made_by,
    };

    let record_line = match serde_json::to_string(&record) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("smm-fast-write: serialization error: {e}");
            std::process::exit(1);
        }
    };

    let decisions_path = smm_dir.join("decisions.jsonl");
    if let Err(e) = append_line(&decisions_path, &record_line) {
        eprintln!("smm-fast-write: failed to write decisions.jsonl: {e}");
        std::process::exit(1);
    }

    // ── Build + append audit entry (best-effort) ─────────────────────────────
    let audit = AuditEntry {
        entry_id: Uuid::new_v4().to_string(),
        timestamp,
        event_type: "decision_recorded".to_string(),
        decision_uuid,
        title: title.clone(),
        source,
    };
    if let Ok(audit_line) = serde_json::to_string(&audit) {
        let lineage_path = smm_dir.join("compliance_lineage.jsonl");
        let _ = append_line(&lineage_path, &audit_line); // ignore audit failures
    }

    // ── Confirm ─────────────────────────────────────────────────────────────
    println!("\u{2713} Decision: {} \u{2014} recorded", title);
    std::process::exit(0);
}
