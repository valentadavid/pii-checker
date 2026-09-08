# llm_client.py
# Abstracts LLM API calls across different providers.
# Currently supports Anthropic, OpenAI (both should be used only for testing), and Ollama.
# Ollama is reached over HTTP. The API key only matters when the endpoint is a remote and requires API key (should not be used in current setup as the prompts are sent unencrypted (using http not https))

# Usage:
#   from llm_client import call_llm, parse_json_response
#   response = call_llm(prompt, provider='anthropic', model='claude-haiku-4-5-20251001')
#   parsed = parse_json_response(response)

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def _load_env_file(path=None):
    """Loads KEY=VALUE lines from config.env into os.environ (without overriding
    variables already set in the real environment)."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.env')
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

OLLAMA_ENDPOINTS = [
    e.strip() for e in os.environ.get('OLLAMA_ENDPOINTS', 'http://localhost:11434').split(',')
    if e.strip()
]
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', '')
OLLAMA_DEBUG = os.environ.get('OLLAMA_DEBUG', 'false').strip().lower() == 'true'

# OLLAMA_THINK controls Ollama's "think" request field: 'true'/'false' send that
# value explicitly; empty or unset (OLLAMA_THINK = None) omits the field entirely,
# so the model does whatever it does by default.
_THINK_RAW = os.environ.get('OLLAMA_THINK', '').strip().lower()
if _THINK_RAW in ('true', '1', 'yes'):
    OLLAMA_THINK = True
elif _THINK_RAW in ('false', '0', 'no'):
    OLLAMA_THINK = False
else:
    OLLAMA_THINK = None

DEFAULT_PROVIDER = os.environ.get('LLM_PROVIDER', 'ollama')
DEFAULT_MODEL = os.environ.get('LLM_MODEL', 'gemma4:e4b')


def check_ollama_reachable(timeout: int = 10) -> bool:
    """Checks that the configured Ollama endpoint is reachable over HTTP."""
    import requests

    if not OLLAMA_ENDPOINTS:
        logger.error("OLLAMA_ENDPOINTS is not set — configure at least one Ollama endpoint")
        return False

    endpoint = OLLAMA_ENDPOINTS[0]
    try:
        response = requests.get(endpoint, headers={"X-API-Key": OLLAMA_API_KEY}, timeout=timeout)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error("Could not reach Ollama at %s: %s", endpoint, e)
        return False


def check_model_available(model: str = DEFAULT_MODEL, timeout: int = 10) -> bool:
    """Checks that the given model is actually pulled on the configured Ollama endpoint.
    Fast — lists installed models, does not run inference."""
    import requests

    if not OLLAMA_ENDPOINTS:
        return False

    endpoint = OLLAMA_ENDPOINTS[0]
    try:
        response = requests.get(f"{endpoint}/api/tags", headers={"X-API-Key": OLLAMA_API_KEY}, timeout=timeout)
        response.raise_for_status()
        available = [m.get("name") or m.get("model") for m in response.json().get("models", [])]
    except requests.exceptions.RequestException as e:
        logger.error("Could not list models at %s: %s", endpoint, e)
        return False

    if model in available:
        return True

    logger.error(
        "Model '%s' is not pulled on %s — available models: %s",
        model, endpoint, ", ".join(available) or "none"
    )
    return False


def check_model_works(model: str = DEFAULT_MODEL, timeout: int = 30) -> bool:
    """Sends a minimal prompt through the model to confirm inference actually works
    (catches a broken/corrupted model that check_model_available can't see).
    Uses the same OLLAMA_THINK setting as the real call, so a model that errors
    on the "think" field is caught here, not mid-batch."""
    import requests

    if not OLLAMA_ENDPOINTS:
        return False

    endpoint = OLLAMA_ENDPOINTS[0]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with just: OK"}],
        "stream": False,
    }
    if OLLAMA_THINK is not None:
        payload["think"] = OLLAMA_THINK

    try:
        response = requests.post(
            f"{endpoint}/api/chat",
            headers={"X-API-Key": OLLAMA_API_KEY},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
    except requests.exceptions.RequestException as e:
        logger.error("Model '%s' failed a test call at %s: %s", model, endpoint, e)
        return False

    if not content.strip():
        logger.error("Model '%s' returned an empty response to a test call", model)
        return False
    return True


def check_anthropic_ready() -> bool:
    """Checks the anthropic package is installed and ANTHROPIC_API_KEY is set."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        logger.error("anthropic package not installed — pip install -r requirements-optional.txt")
        return False

    if not os.environ.get('ANTHROPIC_API_KEY'):
        logger.error("ANTHROPIC_API_KEY is not set")
        return False

    return True


def check_openai_ready() -> bool:
    """Checks the openai package is installed and OPENAI_API_KEY is set."""
    try:
        import openai  # noqa: F401
    except ImportError:
        logger.error("openai package not installed — pip install -r requirements-optional.txt")
        return False

    if not os.environ.get('OPENAI_API_KEY'):
        logger.error("OPENAI_API_KEY is not set")
        return False

    return True


def call_llm(
    prompt: str,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Calls an LLM with the given prompt.
    Returns the response text.

    Supported providers:
    - anthropic
    - openai
    - ollama
    """

    if provider == 'anthropic':
        return _call_anthropic(prompt, model)

    elif provider == 'openai':
        return _call_openai(prompt, model)

    elif provider == 'ollama':
        return _call_ollama(prompt, model)

    else:
        raise ValueError(f"Unknown provider: {provider}")


def _call_anthropic(prompt: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


def _call_openai(prompt: str, model: str) -> str:
    import openai

    client = openai.OpenAI()

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content


_ollama_session = None

def _get_ollama_session():
    import requests
    global _ollama_session
    if _ollama_session is None:
        _ollama_session = requests.Session()
    return _ollama_session


def _call_ollama(prompt: str, model: str) -> str:
    import json as _json

    if not OLLAMA_ENDPOINTS:
        raise RuntimeError("OLLAMA_ENDPOINTS is not set — configure at least one Ollama endpoint")

    endpoint = OLLAMA_ENDPOINTS[0]  # single-endpoint for now; list is a config change away from load balancing
    session = _get_ollama_session()

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": True,
        "keep_alive": -1
    }
    if OLLAMA_THINK is not None:
        payload["think"] = OLLAMA_THINK

    response = session.post(
        f"{endpoint}/api/chat",
        headers={"X-API-Key": OLLAMA_API_KEY},
        json=payload,
        stream=True,
        timeout=180,
    )

    response.raise_for_status()

    if OLLAMA_DEBUG:
        logger.info("[OLLAMA DEBUG] streaming from %s (%s)...", model, endpoint)

    content_parts = []
    for line in response.iter_lines():
        if not line:
            continue
        data = _json.loads(line)
        chunk = data.get("message", {}).get("content", "")
        if chunk:
            content_parts.append(chunk)
            if OLLAMA_DEBUG:
                logger.info("[OLLAMA DEBUG] chunk: %r", chunk)
        if data.get("done"):
            if OLLAMA_DEBUG:
                logger.info("[OLLAMA DEBUG] done. full response: %s", "".join(content_parts))
            break

    return "".join(content_parts)


def parse_json_response(response: str) -> dict | None:
    """
    Parses a JSON response from an LLM.
    Extracts all JSON code blocks and tries the last one first —
    models sometimes self-correct and the final block is the intended answer.
    Falls back to parsing the whole response if no code blocks found.
    """
    # Extract all ```json ... ``` blocks. Model might to reason out loud and
    # output multiple JSON blocks as it self-corrects — consider the last one
    # as the final intended answer. We try in reverse so the last block wins.
    blocks = re.findall(r'```(?:json)?\s*(.*?)```', response, re.DOTALL)

    candidates = [b.strip() for b in reversed(blocks)] if blocks else [response.strip()]

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    logger.error("Failed to parse JSON response\nRaw response: %s", response)
    return None

if __name__ == "__main__":
    import sys
    import requests

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(message)s",
        stream=sys.stdout
    )

    if not OLLAMA_ENDPOINTS:
        print("ERROR: OLLAMA_ENDPOINTS is not set")
        sys.exit(1)

    endpoint = OLLAMA_ENDPOINTS[0]
    print(f"Checking reachability of {endpoint}...")
    resp = requests.get(endpoint, headers={"X-API-Key": OLLAMA_API_KEY}, timeout=10)
    print(f"  -> {resp.status_code}: {resp.text.strip()}")

    test_prompt = """You are a PII detection assistant. Analyze the following column from a dataset and evaluate if it contains personally identifiable information (PII).

Column name: respondent_name
Column label: Respondent full name
Number of rows: 5

Value tabulation:
Alice Johnson      1
Bob Smith          1
Maria Garcia       1
Jean Dupont        1
Yuki Tanaka        1

Respond ONLY with a JSON object in this exact format, no other text:
{
    "reasoning": "one sentence explanation",
    "evaluation": "direct_pii" | "possible_indirect" | "not_pii"
}"""

    print("\nSending prompt to Ollama...")
    response = call_llm(test_prompt)
    print(f"\nRaw response:\n{response}")

    print("\nParsing JSON...")
    parsed = parse_json_response(response)
    if parsed:
        print(f"Evaluation : {parsed['evaluation']}")
        print(f"Reasoning  : {parsed['reasoning']}")
    else:
        print("Failed to parse response")