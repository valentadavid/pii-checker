# test_pii_checker.py
# Unit tests for the whole tool: model selection, folder argument, hashing, output sheets,
# pattern checks and GPS detection. No Ollama, network or real data needed.
# Run with: python -m pytest -q

import builtins

from llm_client import save_model_to_config
from interface import _parse_model_choice


def test_save_model_rewrites_existing_line_and_keeps_rest(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("LLM_PROVIDER=ollama\nLLM_MODEL=gemma4:e4b\n\n# ollama settings\nOLLAMA_ENDPOINTS=http://x:11434\n")
    save_model_to_config("llama3:8b", path=str(cfg))
    assert cfg.read_text() == "LLM_PROVIDER=ollama\nLLM_MODEL=llama3:8b\n\n# ollama settings\nOLLAMA_ENDPOINTS=http://x:11434\n"


def test_save_model_appends_when_line_missing(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("LLM_PROVIDER=ollama\n")
    save_model_to_config("llama3:8b", path=str(cfg))
    assert cfg.read_text() == "LLM_PROVIDER=ollama\nLLM_MODEL=llama3:8b\n"


def test_parse_model_choice_accepts_number():
    assert _parse_model_choice("2", ["a", "b", "c"], current="a") == "b"


def test_parse_model_choice_accepts_name():
    assert _parse_model_choice("c", ["a", "b", "c"], current="a") == "c"


def test_parse_model_choice_empty_returns_current():
    assert _parse_model_choice("", ["a", "b"], current="b") == "b"


def test_parse_model_choice_empty_without_current_is_invalid():
    assert _parse_model_choice("", ["a", "b"], current=None) is None


def test_parse_model_choice_rejects_out_of_range_and_unknown():
    assert _parse_model_choice("9", ["a", "b"], current="a") is None
    assert _parse_model_choice("zzz", ["a", "b"], current="a") is None


def test_resolve_folder_returns_existing_dir(tmp_path):
    from interface import _resolve_folder
    assert _resolve_folder(str(tmp_path)) == str(tmp_path)


def test_resolve_folder_exits_on_non_dir(tmp_path):
    import pytest
    from interface import _resolve_folder
    with pytest.raises(SystemExit):
        _resolve_folder(str(tmp_path / "nope"))


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib
    from find_duplicities import sha256_file
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world" * 5000)  # spans several read chunks
    assert sha256_file(str(f)) == hashlib.sha256(f.read_bytes()).hexdigest()
    assert len(sha256_file(str(f))) == 64


def test_unload_ollama_model_posts_keep_alive_zero(monkeypatch):
    import requests
    import llm_client
    from llm_client import unload_ollama_model

    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(llm_client, "OLLAMA_ENDPOINTS", ["http://ollama.test:11434"])
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or _Resp())

    assert unload_ollama_model("gemma4:e4b") is True
    (url, kw), = calls
    assert url == "http://ollama.test:11434/api/generate"
    assert kw["json"] == {"model": "gemma4:e4b", "keep_alive": 0}


def test_unload_ollama_model_returns_false_on_error(monkeypatch):
    import requests
    import llm_client
    from llm_client import unload_ollama_model

    def _boom(url, **kw):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(llm_client, "OLLAMA_ENDPOINTS", ["http://ollama.test:11434"])
    monkeypatch.setattr(requests, "post", _boom)
    assert unload_ollama_model("gemma4:e4b") is False


def test_version_flag_prints_version(capsys):
    import pytest
    import sys
    import interface
    from version import __version__
    sys.argv = ["interface.py", "--version"]
    with pytest.raises(SystemExit) as e:
        interface.main()
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"interface.py {__version__}"


def test_code_info_reports_https_url_and_commit():
    from main import _code_info
    info = _code_info()
    assert info['code_url'].startswith('https://github.com/') and info['code_url'].endswith('/pii-checker')
    assert len(info['code_commit']) >= 7


def test_save_results_writes_metadata_sheet(tmp_path):
    import pandas as pd
    from main import save_results
    out = tmp_path / "pii_checker_results.xlsx"
    save_results([{'file': 'a.csv', 'evaluation': 'not_pii'}], [], str(out),
                 metadata={'version': '9.9.9', 'started': '2026-01-01 00:00:00',
                           'package_name': 'pkg1', 'n_direct_pii': 2})
    sheets = pd.read_excel(out, sheet_name=None, header=None)
    assert list(sheets) == ['Overview', 'Results', 'Detail', 'Metadata']
    meta = dict(zip(sheets['Metadata'][0], sheets['Metadata'][1]))
    assert meta['version'] == '9.9.9'
    overview = dict(zip(sheets['Overview'][0], sheets['Overview'][1]))
    assert overview['Package'] == 'pkg1'
    assert overview['Flagged: direct PII'] == 2
    assert 'Tool version' not in overview  # version lives in Metadata only
    assert overview['Run finished'] == 'not finished (partial results)'  # no 'ended' yet


def test_pattern_checks_accept_integer_column_names():
    import pandas as pd
    from pii_patterns import evaluate_patterns, is_platform_id_candidate
    # 2-D MATLAB arrays / headerless files give integer column names; used to raise
    # AttributeError: 'int' object has no attribute 'lower'
    assert evaluate_patterns(pd.Series(['abc', 'def']), 1, 1)['floor_met'] is False
    assert is_platform_id_candidate(0, None) is False
    assert is_platform_id_candidate('WorkerId') is True


def test_gps_answer_parsing_ignores_punctuation_and_extra_words(monkeypatch):
    import pandas as pd
    import column_checker
    cases = {'yes': 'direct_pii', 'Yes!': 'direct_pii', 'No.': 'not_pii',
             "no, it's a score": 'not_pii', 'unclear': 'direct_pii', 'maybe': 'direct_pii'}
    for answer, expected in cases.items():
        monkeypatch.setattr(column_checker, '_call_llm_json',
                            lambda *a, answer=answer, **k: {'reasoning': 'r', 'looks_like_coordinate': answer})
        result = column_checker.check_column(pd.Series([45.4215, 40.7128]), 'c', 'c', 2, gps_candidate=True)
        assert result['evaluation'] == expected, answer


def test_count_results_skips_duplicates_and_counts_flags():
    from main import _count_results
    results = [
        {'evaluation': 'direct_pii', 'duplicate_of': ''},
        {'evaluation': 'direct_pii', 'duplicate_of': 'a.csv'},  # copied from a duplicate
        {'evaluation': 'internal_id', 'duplicate_of': ''},
        {'evaluation': 'not_pii', 'duplicate_of': ''},
    ]
    issues = [{'level': 'WARNING'}, {'level': 'ERROR'}, {'level': 'WARNING'}]
    c = _count_results(results, issues)
    assert c['n_columns_checked'] == 3
    assert c['n_direct_pii'] == 2 and c['n_internal_id'] == 1 and c['n_indirect'] == 0
    assert c['n_warnings'] == 2 and c['n_errors'] == 1


def test_llm_usage_accumulates_and_resets():
    import llm_client
    llm_client.reset_usage()
    llm_client._record_usage(prompt_tokens=10, completion_tokens=5, seconds=1.5)
    llm_client._record_usage(prompt_tokens=20, completion_tokens=1, seconds=0.5)
    u = llm_client.get_usage()
    assert u == {'llm_calls': 2, 'llm_prompt_tokens': 30, 'llm_completion_tokens': 6, 'llm_time_s': 2.0}
    llm_client.reset_usage()
    assert llm_client.get_usage()['llm_calls'] == 0
