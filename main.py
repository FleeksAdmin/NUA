#luck bro)

#!/usr/bin/env python3
import argparse
import base64
import html
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

DEEPSEEK_API_KEY = ""
DEEPSEEK_MODEL = "deepseek-chat"
G4F_MODEL_DEFAULT = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """Ти експерт зі шкільних тестів 7 класу України.
Дай відповідь тільки у форматі JSON без пояснень.

Правила:
- Для quiz: рівно 1 правильна відповідь.
- Для multiquiz: від 1 до 4 правильних відповідей.
- Відповідь тільки JSON:
{
  \"question_id\": \"...\",
  \"answer_ids\": [\"...\"]
}
"""

SYSTEM_PROMPT_FL = """Ти експерт зі шкільних тестів 7 класу України.
Поверни відповідь тільки JSON без пояснень.

Важливо:
- НЕ використовуй ID варіантів.
- Поверни точний текст(и) відповіді у тому самому регістрі, як у варіантах.
- Для quiz: рівно 1 відповідь.
- Для multiquiz: від 1 до 4 відповідей.
"""

OCR_SPACE_URL = "https://api.ocr.space/parse/image"
OCR_SPACE_API_KEY = "helloworld"
OCR_SPACE_LANGUAGE = "eng"
G4F_URL = "https://g4f.space/api/nvidia/chat/completions"
OCR_LANGS_DEFAULT = ["eng"]
OCR_ENGINES = [2, 1]
OCR_LANGS_ACTIVE = list(OCR_LANGS_DEFAULT)
G4F_MIN_INTERVAL = 2.8
G4F_MAX_RETRIES = 6
G4F_RETRY_429_SECONDS = 10.0
AUTO_STABLE_SECONDS = 0.85
DEEP_DOUBLE_CHECK = True

_g4f_lock = threading.Lock()
_g4f_next_allowed_at = 0.0
_ocr_cache_lock = threading.Lock()
_ocr_cache: Dict[str, str] = {}
_rapid_ocr_engine = None
_rapid_ocr_lock = threading.Lock()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def decode_har_content(content):
    text = content.get("text") or ""
    if content.get("encoding") == "base64":
        return base64.b64decode(text).decode("utf-8", errors="ignore")
    return text


def html_to_text(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def clean_ids(ids):
    return [str(x).strip().replace("id=", "").strip() for x in ids if str(x).strip()]


def strip_code_fences(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.DOTALL)


def extract_json_block(text: str):
    text = strip_code_fences(text)

    try:
        return json.loads(text)
    except Exception:
        pass

    obj_match = re.search(r"\{[\s\S]*\}", text)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except Exception:
            pass

    arr_match = re.search(r"\[[\s\S]*\]", text)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except Exception:
            pass

    return None


def extract_answer_ids(ai_text: str):
    data = extract_json_block(ai_text)
    if isinstance(data, dict):
        return clean_ids(data.get("answer_ids", []))
    return []


def _listify_texts(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if isinstance(values, (list, tuple)):
        return [str(x).strip() for x in values if str(x).strip()]
    return []


def extract_answer_texts(ai_text: str):
    data = extract_json_block(ai_text)
    if isinstance(data, dict):
        for key in ("answer_texts", "answers", "answer_values", "options_text", "texts"):
            if key in data:
                vals = _listify_texts(data.get(key))
                if vals:
                    return vals
        # fallback when model returns single text
        if "answer" in data and isinstance(data.get("answer"), str):
            val = data.get("answer", "").strip()
            return [val] if val else []
    return []


def extract_session(har):
    for entry in har.get("log", {}).get("entries", []):
        if "/api2/test/sessions/" in entry.get("request", {}).get("url", ""):
            content = decode_har_content(entry["response"].get("content", {}))
            try:
                return json.loads(content)
            except Exception:
                continue
    return None


def build_question_context_block(q):
    qtype = q.get("type", "quiz")
    qtext = html_to_text(q.get("content", ""))
    q_image = (q.get("image") or "").strip()
    q_ocr = run_ocr_for_image(q_image) if q_image else ""
    q_ocr_numeric_hint = _ocr_numeric_hint(q_ocr) if q_ocr else ""

    options = []
    for opt in q.get("options", []):
        opt_text = html_to_text(opt.get("value", ""))
        opt_image = (opt.get("image") or "").strip()
        opt_ocr = run_ocr_for_image(opt_image) if opt_image else ""
        opt_ocr_hint = _ocr_numeric_hint(opt_ocr) if opt_ocr else ""

        details = [opt_text] if opt_text else []
        if opt_image:
            details.append(f"(image: {opt_image})")
        if opt_ocr:
            details.append(f"(OCR: {opt_ocr})")
        if opt_ocr_hint:
            details.append(f"(OCR numeric hint: {opt_ocr_hint})")
        options.append(f"[{opt.get('id')}] {' '.join(details).strip()}")

    image_block = ""
    if q_image:
        image_block += f"Image URL: {q_image}\n"
    if q_ocr:
        image_block += f"Image OCR text: {q_ocr}\n"
    if q_ocr_numeric_hint:
        image_block += f"Image OCR numeric hint: {q_ocr_numeric_hint}\n"

    return (
        f"Question ID: {q.get('id')}\n"
        f"Type: {qtype}\n"
        f"Text: {qtext}\n"
        f"{image_block}"
        "Options:\n"
        f"{chr(10).join(options)}\n"
    )


def _normalize_ocr_text(text: str) -> str:
    text = text or ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _ocr_score(text: str) -> int:
    if not text:
        return 0
    letters = len(re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", text))
    c_letters = len(re.findall(r"[CСcс]", text))
    digits = len(re.findall(r"\d", text))
    math_ops = len(re.findall(r"[+\-*/=()<>^_×÷]", text))
    length = len(text)
    # Numeric-only priority: digits/operators matter more than plain letters.
    return letters * 1 + c_letters * 6 + digits * 4 + math_ops * 4 + length


def _sanitize_math_ocr_text(text: str) -> str:
    text = _normalize_ocr_text(text)
    if not text:
        return ""
    text = text.replace("×", "*").replace("÷", "/")
    text = re.sub(r"[^0-9A-Za-zА-Яа-яІіЇїЄєҐґ+\-*/=()<>^_ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _ocr_numeric_hint(text: str) -> str:
    text = _normalize_ocr_text(text)
    if not text:
        return ""

    digits = len(re.findall(r"\d", text))
    ops = len(re.findall(r"[+\-*/=()<>×÷]", text))
    if digits > 0 or ops == 0:
        return ""

    # OCR often confuses rounded digits and Cyrillic/Latin letters in math expressions.
    repl_map = str.maketrans(
        {
            "O": "0",
            "o": "0",
            "О": "0",
            "о": "0",
            "I": "1",
            "l": "1",
            "|": "1",
            "B": "8",
            "S": "5",
        }
    )
    hint = text.translate(repl_map)
    if hint == text:
        return ""
    if not re.search(r"\d", hint):
        return ""
    return hint


def _parse_ocr_response(ocr_response: requests.Response):
    data = ocr_response.json()
    parsed = data.get("ParsedResults") or []
    raw_text = " ".join((x.get("ParsedText") or "").strip() for x in parsed).strip()
    text = _normalize_ocr_text(raw_text)
    errors = data.get("ErrorMessage") or []
    if isinstance(errors, str):
        errors = [errors]
    return text, errors


def _try_ocr_space(image_url: str, image_bytes: bytes, language: str, engine: int, timeout: int):
    url_text = ""
    file_text = ""
    url_errors = []
    file_errors = []

    try:
        resp = requests.post(
            OCR_SPACE_URL,
            data={
                "apikey": OCR_SPACE_API_KEY,
                "url": image_url,
                "language": language,
                "isOverlayRequired": False,
                "OCREngine": engine,
                "scale": True,
                "detectOrientation": True,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        url_text, url_errors = _parse_ocr_response(resp)
    except Exception:
        pass

    try:
        file_resp = requests.post(
            OCR_SPACE_URL,
            data={
                "apikey": OCR_SPACE_API_KEY,
                "language": language,
                "isOverlayRequired": False,
                "OCREngine": engine,
                "scale": True,
                "detectOrientation": True,
            },
            files={"filename": ("question_image.png", image_bytes)},
            timeout=timeout,
        )
        file_resp.raise_for_status()
        file_text, file_errors = _parse_ocr_response(file_resp)
    except Exception:
        pass

    # If language is invalid, OCR.space reports it in ErrorMessage.
    all_errors = " ".join(url_errors + file_errors).lower()
    is_invalid_lang = "language" in all_errors and "invalid" in all_errors

    best_text = file_text if _ocr_score(file_text) >= _ocr_score(url_text) else url_text
    return best_text, is_invalid_lang


def _get_rapid_ocr_engine():
    global _rapid_ocr_engine
    if _rapid_ocr_engine is not None:
        return _rapid_ocr_engine
    with _rapid_ocr_lock:
        if _rapid_ocr_engine is not None:
            return _rapid_ocr_engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            _rapid_ocr_engine = RapidOCR()
        except Exception:
            _rapid_ocr_engine = False
    return _rapid_ocr_engine


def _run_local_rapid_ocr(image_bytes: bytes) -> str:
    if not image_bytes:
        return ""

    engine = _get_rapid_ocr_engine()
    if not engine:
        return ""

    try:
        from io import BytesIO
        from PIL import Image, ImageEnhance
    except Exception:
        return ""

    try:
        base = Image.open(BytesIO(image_bytes)).convert("L")
    except Exception:
        return ""

    variants = []
    for scale in (2, 3):
        img = base.resize((base.width * scale, base.height * scale), Image.Resampling.LANCZOS)
        variants.append(img)
        variants.append(ImageEnhance.Contrast(img).enhance(2.2))
        variants.append(ImageEnhance.Contrast(img).enhance(2.8).point(lambda p: 255 if p > 165 else 0))
        variants.append(ImageEnhance.Contrast(img).enhance(2.8).point(lambda p: 255 if p > 185 else 0))

    best = ""
    best_score = 0
    for img in variants:
        try:
            result, _ = engine(img)
        except Exception:
            continue
        if not result:
            continue
        text = _sanitize_math_ocr_text(" ".join(str(x[1]).strip() for x in result if len(x) > 1))
        score = _ocr_score(text)
        if score > best_score:
            best_score = score
            best = text
        if best_score >= 14:
            break
    return best


def run_ocr_for_image(image_url: str, timeout: int = 30, language_candidates=None) -> str:
    image_url = (image_url or "").strip()
    if not image_url:
        return ""

    with _ocr_cache_lock:
        if image_url in _ocr_cache:
            return _ocr_cache[image_url]

    try:
        image_resp = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://naurok.com.ua/"},
            timeout=timeout,
        )
        image_resp.raise_for_status()
        image_bytes = image_resp.content
    except Exception:
        image_bytes = b""

    local_text = _run_local_rapid_ocr(image_bytes)
    local_score = _ocr_score(local_text)

    langs = language_candidates or OCR_LANGS_ACTIVE
    langs = [x.strip() for x in langs if str(x).strip()]
    if not langs:
        langs = [OCR_SPACE_LANGUAGE]

    # Numeric-only + speed mode: local OCR only (remote OCR was slow and rate-limited).
    ocr_text = local_text if local_score > 0 else ""

    with _ocr_cache_lock:
        _ocr_cache[image_url] = ocr_text
    return ocr_text


def build_single_question_prompt(q):
    return (
        build_question_context_block(q)
        + "\n"
        "Return ONLY JSON in this exact schema:\n"
        "{\n"
        f"  \"question_id\": \"{q.get('id')}\",\n"
        "  \"answer_ids\": [\"<option_id>\"]\n"
        "}\n"
    )


def build_single_question_prompt_fl(q):
    return (
        build_question_context_block(q)
        + "\n"
        "Return ONLY JSON in this exact schema (use exact option text, preserve case):\n"
        "{\n"
        f"  \"question_id\": \"{q.get('id')}\",\n"
        "  \"answer_texts\": [\"<exact option text>\"]\n"
        "}\n"
    )


def _norm_answer_text(s: str) -> str:
    s = html_to_text(str(s or ""))
    s = s.replace(",", ".")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def map_answer_texts_to_ids(question, answer_texts):
    answer_texts = [x for x in _listify_texts(answer_texts) if x]
    if not answer_texts:
        return []

    option_map = []
    for opt in question.get("options", []):
        oid = str(opt.get("id"))
        txt = html_to_text(opt.get("value", ""))
        candidates = set()
        n = _norm_answer_text(txt)
        if n:
            candidates.add(n)
        ocr = _normalize_ocr_text(run_ocr_for_image((opt.get("image") or "").strip()))
        on = _norm_answer_text(ocr)
        if on:
            candidates.add(on)
        option_map.append((oid, candidates))

    picked = []
    used = set()
    for raw in answer_texts:
        a = _norm_answer_text(raw)
        if not a:
            continue
        best = None
        best_score = -1
        for oid, cands in option_map:
            if oid in used:
                continue
            score = -1
            if a in cands:
                score = 1000
            else:
                for c in cands:
                    if not c:
                        continue
                    if a in c or c in a:
                        score = max(score, min(len(a), len(c)))
            if score > best_score:
                best_score = score
                best = oid
        if best is not None and best_score >= 2:
            picked.append(best)
            used.add(best)
    return clean_ids(picked)


def _extract_numeric_options(question):
    parsed = []
    for opt in question.get("options", []):
        oid = str(opt.get("id"))
        txt = html_to_text(opt.get("value", ""))
        norm = txt.replace(" ", "").replace(",", ".")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", norm):
            try:
                val = float(norm)
            except Exception:
                continue
            parsed.append((oid, val))
    return parsed


def infer_answer_from_image_math(question) -> Optional[list]:
    q_image = (question.get("image") or "").strip()
    if not q_image:
        return None

    numeric_options = _extract_numeric_options(question)
    if not numeric_options:
        return None

    ocr = run_ocr_for_image(q_image)
    if not ocr:
        return None

    text = _normalize_ocr_text(ocr)
    c_count = len(re.findall(r"[CС]", text))
    plus_count = len(re.findall(r"\+", text))
    term_count = max(c_count, plus_count + 1)
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if term_count < 2 or not nums:
        return None

    option_vals = {val: oid for oid, val in numeric_options}
    n_candidates = list(sorted(set(nums)))
    for raw in nums:
        if raw >= 10:
            tail = int(str(raw)[-1])
            if 1 <= tail <= 9 and (tail + 1) not in n_candidates:
                n_candidates.append(tail + 1)
    if term_count >= 3 and len(set(nums)) == 1:
        # OCR often catches only middle exponent in expressions like C^4_6 + C^5_6 + C^6_6.
        only = nums[0]
        if only + 1 not in n_candidates:
            n_candidates = [only + 1] + n_candidates

    for n in n_candidates:
        if n <= 0 or n > 70:
            continue

        candidates = set()
        m = max(2, term_count)
        try:
            # Upper tail: C(n,n-m+1)+...+C(n,n)
            candidates.add(float(sum(math.comb(n, k) for k in range(max(0, n - m + 1), n + 1))))
            # Lower tail: C(n,0)+...+C(n,m-1)
            candidates.add(float(sum(math.comb(n, k) for k in range(0, min(m, n + 1)))))
        except Exception:
            continue

        hits = [v for v in candidates if v in option_vals]
        if len(hits) == 1:
            return [option_vals[hits[0]]]
    return None


def parse_sse_content(raw_text: str) -> str:
    parts = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue

        choices = obj.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        chunk = delta.get("content")
        if chunk:
            parts.append(chunk)

    return "".join(parts).strip()


def build_batch_questions_prompt(questions, use_fl=False):
    blocks = []
    for i, q in enumerate(questions, start=1):
        blocks.append(f"### QUESTION {i}\n{build_question_context_block(q)}")

    if use_fl:
        schema = "{\"question_id\":\"...\",\"answer_texts\":[\"<exact option text>\"]}"
    else:
        schema = "{\"question_id\":\"...\",\"answer_ids\":[\"...\"]}"

    return (
        "Виріши всі питання нижче.\n"
        "Поверни ТІЛЬКИ JSON-об'єкти, по одному на рядок (NDJSON), без markdown і пояснень.\n"
        f"Кожен рядок строго такого виду:\n{schema}\n\n"
        + "\n".join(blocks)
    )


def _extract_complete_json_objects(buffer: str):
    objs = []
    start = -1
    depth = 0
    in_str = False
    esc = False
    last_end = 0

    for i, ch in enumerate(buffer):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
            continue

        if ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    objs.append(buffer[start : i + 1])
                    last_end = i + 1
                    start = -1

    tail = buffer[last_end:] if last_end > 0 else buffer
    if len(tail) > 12000:
        tail = tail[-12000:]
    return objs, tail


def _iter_sse_delta_text(resp: requests.Response):
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        choices = obj.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        chunk = delta.get("content")
        if chunk:
            yield chunk


def _ingest_stream_chunk(
    chunk,
    pending_buf,
    answers_map,
    lock,
    known_qids,
    solved_qids,
    total,
    question_by_qid,
    use_fl=False,
):
    pending_buf += chunk
    objs, pending_buf = _extract_complete_json_objects(pending_buf)
    for obj_text in objs:
        try:
            data = json.loads(obj_text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        qid = str(data.get("question_id", "")).strip()
        if qid not in known_qids:
            continue
        if use_fl:
            answer_texts = extract_answer_texts(obj_text)
            answer_ids = map_answer_texts_to_ids(question_by_qid.get(qid, {}), answer_texts)
        else:
            answer_ids = clean_ids(data.get("answer_ids", []))
        with lock:
            answers_map[qid] = answer_ids
        if qid not in solved_qids:
            solved_qids.add(qid)
        print(f"[AI-stream] {len(solved_qids)}/{total} qid={qid} -> {answer_ids}")
    return pending_buf


def ask_batch_questions_stream(questions, use_gpt, gpt_model, answers_map, lock, use_fl=False, timeout=120):
    prompt = build_batch_questions_prompt(questions, use_fl=use_fl)
    known_qids = {str(q.get("id")) for q in questions}
    question_by_qid = {str(q.get("id")): q for q in questions}
    solved_qids = set()
    pending_buf = ""
    system_prompt = SYSTEM_PROMPT_FL if use_fl else SYSTEM_PROMPT

    if use_gpt:
        url = G4F_URL
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://g4f.dev",
            "Referer": "https://g4f.dev/",
            "User-Agent": "Mozilla/5.0",
        }
        payload = {
            "model": gpt_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
        }
    else:
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 4000,
            "stream": True,
        }

    attempts = G4F_MAX_RETRIES + 1 if use_gpt else 2
    last_error = None
    for attempt in range(attempts):
        if use_gpt:
            _acquire_g4f_slot(min_interval=G4F_MIN_INTERVAL)
        try:
            with requests.post(url, headers=headers, json=payload, timeout=timeout, stream=True) as resp:
                if resp.status_code == 429:
                    raise requests.HTTPError("429 Too Many Requests", response=resp)
                resp.raise_for_status()
                content_type = (resp.headers.get("content-type") or "").lower()

                if "text/event-stream" in content_type:
                    for chunk in _iter_sse_delta_text(resp):
                        pending_buf = _ingest_stream_chunk(
                            chunk,
                            pending_buf,
                            answers_map,
                            lock,
                            known_qids,
                            solved_qids,
                            len(questions),
                            question_by_qid,
                            use_fl=use_fl,
                        )
                else:
                    full_text = resp.text
                    pending_buf = _ingest_stream_chunk(
                        full_text,
                        pending_buf,
                        answers_map,
                        lock,
                        known_qids,
                        solved_qids,
                        len(questions),
                        question_by_qid,
                        use_fl=use_fl,
                    )
            break
        except Exception as e:
            last_error = e
            if not use_gpt or attempt >= attempts - 1:
                break
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code == 429:
                sleep_for = G4F_RETRY_429_SECONDS
            else:
                sleep_for = min(12.0, 1.4 * (2 ** attempt))
            print(f"[AI-batch] retry {attempt + 1}/{attempts - 1} in {sleep_for:.1f}s due to: {e}")
            time.sleep(sleep_for)

    # Final parse attempt from any leftover text.
    pending_buf = _ingest_stream_chunk(
        "",
        pending_buf,
        answers_map,
        lock,
        known_qids,
        solved_qids,
        len(questions),
        question_by_qid,
        use_fl=use_fl,
    )

    if last_error and not solved_qids:
        raise last_error
    return solved_qids


def get_content_from_response(resp: requests.Response) -> str:
    content_type = (resp.headers.get("content-type") or "").lower()

    if "text/event-stream" in content_type:
        return parse_sse_content(resp.text)

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return resp.text


def ask_question_deepseek(question, timeout=40, use_fl=False):
    prompt = build_single_question_prompt_fl(question) if use_fl else build_single_question_prompt(question)
    system_prompt = SYSTEM_PROMPT_FL if use_fl else SYSTEM_PROMPT
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 280,
            "stream": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = get_content_from_response(resp)
    if use_fl:
        return map_answer_texts_to_ids(question, extract_answer_texts(content))
    return extract_answer_ids(content)


def ask_question_gpt(
    question,
    model,
    timeout=40,
    min_interval=G4F_MIN_INTERVAL,
    max_retries=G4F_MAX_RETRIES,
    use_fl=False,
):
    prompt = build_single_question_prompt_fl(question) if use_fl else build_single_question_prompt(question)
    system_prompt = SYSTEM_PROMPT_FL if use_fl else SYSTEM_PROMPT
    return ask_gpt_with_backoff(
        prompt=prompt,
        model=model,
        timeout=timeout,
        min_interval=min_interval,
        max_retries=max_retries,
        system_prompt=system_prompt,
        question=question,
        use_fl=use_fl,
    )


def _acquire_g4f_slot(min_interval: float):
    global _g4f_next_allowed_at
    while True:
        with _g4f_lock:
            now = time.monotonic()
            if now >= _g4f_next_allowed_at:
                _g4f_next_allowed_at = now + min_interval
                return
            wait_for = _g4f_next_allowed_at - now
        if wait_for > 0:
            time.sleep(wait_for)


def _parse_retry_after(headers) -> Optional[float]:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except Exception:
        return None


def ask_gpt_with_backoff(
    prompt,
    model,
    timeout=40,
    min_interval=G4F_MIN_INTERVAL,
    max_retries=G4F_MAX_RETRIES,
    system_prompt=SYSTEM_PROMPT,
    question=None,
    use_fl=False,
):
    last_error = None
    for attempt in range(max_retries + 1):
        _acquire_g4f_slot(min_interval=min_interval)
        try:
            resp = requests.post(
                G4F_URL,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://g4f.dev",
                    "Referer": "https://g4f.dev/",
                    "User-Agent": "Mozilla/5.0",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                },
                timeout=timeout,
            )
            if resp.status_code == 429:
                raise requests.HTTPError("429 Too Many Requests", response=resp) from None
            resp.raise_for_status()
            content = get_content_from_response(resp)
            if use_fl and question is not None:
                return map_answer_texts_to_ids(question, extract_answer_texts(content))
            return extract_answer_ids(content)
        except Exception as e:
            last_error = e
            if attempt >= max_retries:
                break

            retry_after = None
            is_429 = False
            if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
                code = e.response.status_code
                if code not in (429, 500, 502, 503, 504):
                    raise
                is_429 = code == 429
                retry_after = _parse_retry_after(e.response.headers)
            elif isinstance(e, requests.RequestException):
                retry_after = None
            else:
                raise

            if is_429:
                sleep_for = G4F_RETRY_429_SECONDS
            else:
                backoff = min(20.0, 1.4 * (2 ** attempt))
                sleep_for = max(backoff, retry_after or 0.0)
            print(f"[AI] retry {attempt + 1}/{max_retries} in {sleep_for:.1f}s due to: {e}")
            time.sleep(sleep_for)

    raise last_error if last_error else RuntimeError("g4f request failed")


def resolve_answers_worker(
    questions,
    use_gpt,
    gpt_model,
    answers_map,
    lock,
    use_fl=False,
):
    total = len(questions)
    unresolved = []

    # 1) Fast local OCR/math heuristics first.
    for idx, q in enumerate(questions, start=1):
        qid = str(q.get("id"))
        heuristic = infer_answer_from_image_math(q)
        if heuristic:
            with lock:
                answers_map[qid] = heuristic
            print(f"[OCR] {idx}/{total} qid={qid} heuristic -> {heuristic}")
        else:
            unresolved.append(q)

    # 2) One batch request for all unresolved questions with streaming incremental parsing.
    if unresolved:
        try:
            solved = ask_batch_questions_stream(
                questions=unresolved,
                use_gpt=use_gpt,
                gpt_model=gpt_model,
                answers_map=answers_map,
                lock=lock,
                use_fl=use_fl,
            )
            print(f"[AI-batch] solved={len(solved)}/{len(unresolved)}")
        except Exception as e:
            print(f"[AI-batch] error: {e}")

    # 3) Fallback per-question only for those still unresolved.
    for idx, q in enumerate(questions, start=1):
        qid = str(q.get("id"))
        with lock:
            ready = qid in answers_map and bool(answers_map[qid])
        if ready:
            continue
        try:
            if use_gpt:
                answer_ids = ask_question_gpt(
                    q,
                    model=gpt_model,
                    min_interval=G4F_MIN_INTERVAL,
                    max_retries=G4F_MAX_RETRIES,
                    use_fl=use_fl,
                )
            else:
                answer_ids = ask_question_deepseek(q, use_fl=use_fl)
                if DEEP_DOUBLE_CHECK:
                    second = ask_question_deepseek(q, use_fl=use_fl)
                    if clean_ids(answer_ids) != clean_ids(second):
                        print(f"[DEEP] {idx}/{total} qid={qid} mismatch first={answer_ids} second={second}")
                        answer_ids = second if second else answer_ids
        except Exception as e:
            answer_ids = []
            print(f"[AI-fallback] {idx}/{total} qid={qid} error: {e}")
        with lock:
            answers_map[qid] = answer_ids
        print(f"[AI-fallback] {idx}/{total} qid={qid} -> {answer_ids}")


def inject_watermark(driver):
    driver.execute_script("""
    if (document.getElementById('by-fleeks')) return;
    const d = document.createElement('div');
    d.id = 'by-fleeks';
    d.textContent = 'BY FLEEKS';
    d.style.cssText = `
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%) rotate(-10deg);
        font-size: min(11vw, 110px);
        font-weight: 900;
        letter-spacing: 0.14em;
        pointer-events: none;
        z-index: 999999;
        white-space: nowrap;
        color: #000000;
        opacity: 0.4;
        text-shadow: none;
        mix-blend-mode: normal;
        user-select: none;
    `;
    document.body.appendChild(d);
    """)


def highlight(driver, el):
    driver.execute_script(
        """
        arguments[0].classList.add('fleeks-highlight-correct');
        arguments[0].style.border = '3px solid #ff2f5b';
        arguments[0].style.boxShadow = '0 0 0 4px rgba(255,47,91,0.35), 0 0 18px rgba(255,47,91,0.55)';
        arguments[0].style.backgroundColor = 'rgba(255,47,91,0.15)';
        arguments[0].style.borderRadius = '12px';
        arguments[0].style.transition = 'all .18s ease';
        """,
        el,
    )


def clear_highlights(driver):
    driver.execute_script(
        """
        document.querySelectorAll('.fleeks-highlight-correct').forEach(e => {
            e.classList.remove('fleeks-highlight-correct');
            e.style.border = '';
            e.style.boxShadow = '';
            e.style.backgroundColor = '';
            e.style.borderRadius = '';
            e.style.transition = '';
            e.style.transform = '';
        });
        """
    )


def question_already_answered(driver):
    return driver.execute_script(
        """
        const optionBoxes = Array.from(document.querySelectorAll('.question-option-inner'));
        if (!optionBoxes.length) return false;

        const byInput = optionBoxes.some(el =>
            el.querySelector('input[type="radio"]:checked, input[type="checkbox"]:checked')
        );
        if (byInput) return true;

        const byAria = optionBoxes.some(el =>
            el.getAttribute('aria-checked') === 'true' ||
            (el.closest('[aria-checked="true"]') !== null)
        );
        if (byAria) return true;

        const byClass = optionBoxes.some(el => {
            const cls = (el.className || '').toLowerCase();
            const parentCls = (el.parentElement && el.parentElement.className ? el.parentElement.className : '').toLowerCase();
            return ['selected', 'active', 'checked', 'chosen', 'picked', 'answered']
                .some(k => cls.includes(k) || parentCls.includes(k));
        });
        return byClass;
        """
    )


def find_current_question_id(driver, question_index):
    body = driver.find_element(By.TAG_NAME, "body").text.lower()
    for qid, q in question_index.items():
        probe = q["q_lower"][:65]
        if probe and probe in body:
            return qid
    return None


def build_correct_set(question, answer_ids):
    correct = set()
    answer_ids = set(answer_ids)
    for opt in question.get("options", []):
        oid = str(opt.get("id"))
        if oid in answer_ids:
            txt = html_to_text(opt.get("value", "")).lower().strip()
            if txt:
                correct.add(txt)
    return correct


def _norm_option_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace(",", ".")
    return text


def get_current_question_position(driver):
    try:
        idx = driver.execute_script(
            """
            const el = document.querySelector('.currentActiveQuestion');
            if (!el) return null;
            const n = parseInt((el.textContent || '').trim(), 10);
            return Number.isFinite(n) ? n : null;
            """
        )
        if isinstance(idx, int) and idx > 0:
            return idx
    except Exception:
        return None
    return None


def click_answers(driver, question, answer_ids):
    answer_set = set(clean_ids(answer_ids))
    if not answer_set:
        return False

    qtype = (question.get("type") or "").strip().lower()
    if qtype == "quiz" and len(answer_set) != 1:
        print(f"[AUTO] skip qid={question.get('id')} invalid quiz answers={sorted(answer_set)}")
        return False

    options = question.get("options", [])
    nodes = driver.find_elements(By.CSS_SELECTOR, ".question-option-inner")
    if len(nodes) < len(options):
        print(f"[AUTO] skip qid={question.get('id')} dom options not ready ({len(nodes)}/{len(options)})")
        return False

    # Build target texts for safer mapping than raw index clicks.
    target_texts = {}
    for opt in options:
        oid = str(opt.get("id"))
        if oid in answer_set:
            target_texts[oid] = _norm_option_text(html_to_text(opt.get("value", "")))

    dom_nodes_by_text = {}
    for node in nodes:
        t = _norm_option_text(node.text)
        if t:
            dom_nodes_by_text.setdefault(t, []).append(node)

    clicked = False

    for opt_idx, opt in enumerate(options):
        oid = str(opt.get("id"))
        if oid not in answer_set:
            continue

        el = None
        tgt = target_texts.get(oid, "")
        if tgt and tgt in dom_nodes_by_text and len(dom_nodes_by_text[tgt]) == 1:
            el = dom_nodes_by_text[tgt][0]
        elif opt_idx < len(nodes):
            el = nodes[opt_idx]
        if el is None:
            continue

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            driver.execute_script("arguments[0].click();", el)
            clicked = True
            time.sleep(0.25)
        except Exception:
            continue

    if clicked and question.get("type") == "multiquiz":
        try:
            save_btn = driver.find_element(By.CSS_SELECTOR, ".test-multiquiz-save-button")
            time.sleep(0.2)
            driver.execute_script("arguments[0].click();", save_btn)
        except Exception:
            pass

    return clicked


def build_driver():
    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', {get: () => ['uk-UA','uk','en-US','en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4]});
                """
            },
        )
    except Exception:
        pass
    return driver


def main():
    global OCR_LANGS_ACTIVE, DEEPSEEK_API_KEY
    parser = argparse.ArgumentParser()
    parser.add_argument("--har", default="naurok.com.ua.har")
    parser.add_argument("--url", required=True)
    provider = parser.add_mutually_exclusive_group(required=True)
    provider.add_argument("--gpt", action="store_true", help="Use g4f.space Nvidia endpoint")
    provider.add_argument("--deep", action="store_true", help="Use DeepSeek endpoint")
    parser.add_argument("--gpt-model", default=G4F_MODEL_DEFAULT)
    parser.add_argument(
        "--deep-key",
        default="",
        help="DeepSeek API key. If omitted, DEEPSEEK_API_KEY environment variable is used.",
    )
    parser.add_argument(
        "--fl",
        action="store_true",
        help="Force LLM to return answer text (not option ids), then map text to ids locally",
    )
    parser.add_argument("--auto", action="store_true", help="Auto-click AI answers")
    parser.add_argument(
        "--ocr-langs",
        default="eng",
        help="Comma-separated OCR language candidates, e.g. eng",
    )
    args = parser.parse_args()

    cli_deep_key = (args.deep_key or "").strip()
    env_deep_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    DEEPSEEK_API_KEY = cli_deep_key or env_deep_key

    if args.deep and not DEEPSEEK_API_KEY:
        print("DeepSeek API key is required. Use --deep-key or DEEPSEEK_API_KEY.")
        raise SystemExit(1)

    OCR_LANGS_ACTIVE = [x.strip() for x in args.ocr_langs.split(",") if x.strip()]
    if not OCR_LANGS_ACTIVE:
        OCR_LANGS_ACTIVE = list(OCR_LANGS_DEFAULT)

    print("Loading HAR...")
    har = load_json(Path(args.har))
    session = extract_session(har)

    if not session or "questions" not in session:
        print("Questions not found in HAR")
        return

    questions = session["questions"]
    question_index = {
        str(q["id"]): {
            "question": q,
            "q_lower": html_to_text(q.get("content", "")).lower().strip(),
        }
        for q in questions
    }

    print("Starting browser now...")
    driver = build_driver()
    driver.get(args.url)
    time.sleep(3)
    inject_watermark(driver)

    answers_map = {}
    answers_lock = threading.Lock()

    provider_name = f"g4f ({args.gpt_model})" if args.gpt else f"deepseek ({DEEPSEEK_MODEL})"
    if args.fl:
        provider_name += " + fl"
    print(f"AI provider: {provider_name}")

    worker = threading.Thread(
        target=resolve_answers_worker,
        args=(
            questions,
            args.gpt,
            args.gpt_model,
            answers_map,
            answers_lock,
            args.fl,
        ),
        daemon=True,
    )
    worker.start()

    last_qid = None
    qid_seen_at = {}
    highlighted_qid = None
    auto_clicked_qids = set()
    ordered_qids = [str(q.get("id")) for q in questions]

    while True:
        try:
            time.sleep(0.25)
            current_qid = None
            current_position = get_current_question_position(driver)
            if current_position and 1 <= current_position <= len(ordered_qids):
                current_qid = ordered_qids[current_position - 1]
            if not current_qid:
                current_qid = find_current_question_id(driver, question_index)
            if not current_qid:
                continue

            if current_qid != last_qid:
                clear_highlights(driver)
                highlighted_qid = None
                last_qid = current_qid
                qid_seen_at[current_qid] = time.monotonic()

            if question_already_answered(driver):
                if highlighted_qid == current_qid:
                    clear_highlights(driver)
                    highlighted_qid = None
                continue

            with answers_lock:
                current_answer_ids = answers_map.get(current_qid)

            if not current_answer_ids:
                continue

            question = question_index[current_qid]["question"]
            if args.auto and current_qid not in auto_clicked_qids:
                seen_at = qid_seen_at.get(current_qid, time.monotonic())
                if time.monotonic() - seen_at < AUTO_STABLE_SECONDS:
                    continue
                if click_answers(driver, question, current_answer_ids):
                    auto_clicked_qids.add(current_qid)
                    print(f"[AUTO] clicked qid={current_qid} -> {current_answer_ids}")
                continue

            if highlighted_qid == current_qid:
                continue

            correct_set = build_correct_set(question, current_answer_ids)
            if not correct_set:
                continue

            clear_highlights(driver)
            for el in driver.find_elements(By.CSS_SELECTOR, ".question-option-inner"):
                text = el.text.lower().strip()
                if any(c in text for c in correct_set):
                    highlight(driver, el)

            highlighted_qid = current_qid

        except Exception as e:
            if "detached" in str(e).lower():
                print("Browser closed")
                break


if __name__ == "__main__":
    main()
