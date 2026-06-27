# QA fixture sources

These fixtures are static UTF-8, no-BOM inputs for offline QA tests.
They do not contain real tokens, secrets, server addresses, phone numbers, or viewing passwords.

## Files

- `single_window_required_utf8.json`
  - Source: `qa_artifacts/run_rag_10windows_10turns_utf8.py`, constant `WINDOWS[0]["turns"]`.
  - Source sha256 at generation time: `52ec51b471c2b0aa247c4553e77f1519763031e4f417b6603ef0b03d42aad4bd`.
  - Fixture sha256 at generation time: `cf89f565c83afb1b65e00f4ad168189b56cd8d7e34570be3e506b7dc3f04ce97`.

- `test_text_full_utf8.json`
  - Source: `qa_artifacts/run_rag_10windows_10turns_utf8.py`, flattened `WINDOWS[*]["turns"]`.
  - Source sha256 at generation time: `52ec51b471c2b0aa247c4553e77f1519763031e4f417b6603ef0b03d42aad4bd`.
  - Fixture sha256 at generation time: `a3a60f0ab240e38d0318b329b39570b7c658a7456646ac6a72971e880e9449d4`.

- `real_server_dialogues_sanitized.json`
  - Optional source: server `data/kf_dialogue_events.jsonl` or an exported server dialogue/event log.
  - Must be generated with `scripts/export_real_dialogue_fixture.py` so raw customer ids, phone numbers, viewing passwords, tokens, and long runtime identifiers are removed before the file enters tests.
  - The raw server log must not be committed.

- `historical_failures_synthetic_sanitized.json`
  - Source: synthetic sanitized replay cases for the L4 historical failure gate.
  - These cases are mechanism fixtures, not real server evidence.
  - They must not contain phone numbers, viewing passwords, tokens, long hashes, raw signatures, server ids, or raw customer identifiers.

## Generation command

Run from the repository root with UTF-8 enabled:

```powershell
$env:PYTHONIOENCODING='utf-8'
python - <<'PY'
import ast, json
from pathlib import Path

repo = Path.cwd()
source = repo / 'qa_artifacts' / 'run_rag_10windows_10turns_utf8.py'

def literal_assign(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', '') == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, 'id', '') == name:
                    return ast.literal_eval(node.value)
    raise KeyError(name)

windows = literal_assign(source, 'WINDOWS')
single_questions = list(windows[0]['turns'])
full_questions = [turn for window in windows for turn in window['turns']]
fixture_dir = repo / 'tests' / 'fixtures' / 'qa'
fixture_dir.mkdir(parents=True, exist_ok=True)
(fixture_dir / 'single_window_required_utf8.json').write_text(
    json.dumps({'questions': single_questions}, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
(fixture_dir / 'test_text_full_utf8.json').write_text(
    json.dumps({'questions': full_questions}, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
PY
```

## Real server dialogue import

After a read-only server log export is available locally, generate the sanitized replay fixture:

```powershell
$env:PYTHONIOENCODING='utf-8'
python scripts/export_real_dialogue_fixture.py `
  --input tmp/server_dialogues/kf_dialogue_events.jsonl `
  --output tests/fixtures/qa/real_server_dialogues_sanitized.json `
  --limit-windows 20
python -m pytest -q tests/test_real_dialogue_fixtures.py
```

The fixture stores only sanitized customer turns. Bot replies from old logs are intentionally not used as expected answers, because current correctness must be judged against the latest inventory/tools rather than stale historical output.
