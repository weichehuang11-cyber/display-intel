"""AI 模型介面：同一套程式可切換免費或付費模型。

provider 選項（在 config.yaml 的 llm.provider 設定）：
- gemini：Google AI Studio 免費額度（預設），需 GEMINI_API_KEY
- anthropic：Claude 付費 API，品質最好，需 ANTHROPIC_API_KEY
（GitHub Models 已於 2026-07-30 停止服務，不再支援）

所有 provider 都回傳 dict（JSON）。內建節流與 429 重試，免費額度用完時會停止並回報，不會讓流程崩潰。
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .config import env

log = logging.getLogger(__name__)

OPENAI_COMPATIBLE = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", ("GEMINI_API_KEY",)),
}


class BudgetExceeded(RuntimeError):
    """今天的呼叫次數用完了（設定上限或免費額度）。"""


class OutputTruncated(RuntimeError):
    """模型輸出超過長度上限被截斷（一次送太多文章），呼叫端應拆小批次重試。"""


def parse_json(text: str) -> dict:
    """容忍模型在 JSON 外包了 ``` 或多餘文字。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型回覆中找不到 JSON")
    return json.loads(text[start:end + 1])


class LLM:
    def __init__(self, provider: str, model: str, max_calls: int = 50,
                 min_interval_s: float = 0.0, max_output_tokens: int = 4000):
        self.provider = provider
        self.model = model
        self.max_calls = max_calls
        self.min_interval_s = min_interval_s
        self.max_output_tokens = max_output_tokens
        self.calls = 0
        self._last = 0.0
        self.fallbacks: list[str] = []  # 免費額度是各模型分開算：這個模型額度用完時依序改用
        self.switched: list[str] = []
        if provider == "anthropic":
            import anthropic
            self._client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
        elif provider == "github_models":
            raise RuntimeError("GitHub Models 已於 2026-07-30 停止服務，請把 config.yaml 的 provider 改成 gemini")
        elif provider in OPENAI_COMPATIBLE:
            url, keys = OPENAI_COMPATIBLE[provider]
            token = next((env(k) for k in keys if env(k)), "")
            if not token:
                raise RuntimeError(f"provider={provider} 需要環境變數 {' 或 '.join(keys)}")
            self._url = url
            self._http = httpx.Client(timeout=httpx.Timeout(120.0), follow_redirects=True,
                                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        else:
            raise ValueError(f"不支援的 provider：{provider}")

    @classmethod
    def from_config(cls, llm_cfg: dict, role: str = "extract") -> "LLM":
        return cls(provider=llm_cfg.get("provider", "gemini"),
                   model=llm_cfg.get(f"{role}_model") or llm_cfg["extract_model"],
                   max_calls=llm_cfg.get("max_calls_per_run", 50),
                   min_interval_s=llm_cfg.get("min_seconds_between_calls", 0),
                   max_output_tokens=llm_cfg.get("max_output_tokens", 4000))

    def _pace(self):
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"已達今日呼叫上限 {self.max_calls} 次")
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1

    def complete_json(self, system: str, user: str, retries: int = 4) -> dict:
        for attempt in range(retries):
            self._pace()
            try:
                text = self._anthropic(system, user) if self.provider == "anthropic" else self._openai(system, user)
                return parse_json(text)
            except BudgetExceeded:
                raise
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    retry_after = float(exc.response.headers.get("retry-after", 0) or 0)
                    if retry_after > 600 or attempt == retries - 1:
                        # 等超過 10 分鐘通常代表今天的免費額度用完了；還有備用模型就換一個繼續
                        if self.fallbacks:
                            old, self.model = self.model, self.fallbacks.pop(0)
                            self.switched.append(f"{old} → {self.model}")
                            log.warning("%s 今日免費額度用完，改用 %s 繼續", old, self.model)
                            return self.complete_json(system, user, retries)
                        raise BudgetExceeded(f"免費額度已用完（429，需等待 {retry_after:.0f} 秒）") from exc
                    time.sleep(max(retry_after, 20 * (attempt + 1)))
                    continue
                if status >= 500 and attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise
            except OutputTruncated:
                raise
            except (ValueError, json.JSONDecodeError) as exc:
                log.warning("模型回覆不是有效 JSON（第 %d 次）：%s", attempt + 1, exc)
                if attempt >= 1:  # 同樣的請求重試一次就好，避免浪費免費額度
                    raise
            except Exception as exc:  # 網路或 SDK 錯誤
                if "ratelimit" in type(exc).__name__.lower() and attempt < retries - 1:
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("模型呼叫失敗")

    def _openai(self, system: str, user: str) -> str:
        r = self._http.post(self._url, json={
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        })
        if r.status_code >= 400:
            # 把伺服器的錯誤說明帶進訊息，方便除錯
            raise httpx.HTTPStatusError(f"{r.status_code}：{r.text[:400]}", request=r.request, response=r)
        try:
            choice = r.json()["choices"][0]
        except Exception:
            raise RuntimeError(f"AI 服務回覆格式異常（HTTP {r.status_code}）：{r.text[:300]!r}") from None
        content = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") == "length":
            raise OutputTruncated("模型輸出被截斷")
        if not content:
            raise ValueError(f"模型沒有回覆內容（finish_reason={choice.get('finish_reason')}）")
        return content

    def _anthropic(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_output_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        if getattr(msg, "stop_reason", "") == "max_tokens":
            raise OutputTruncated("模型輸出被截斷")
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def probe(self) -> str:
        """執行前先用一個極短的請求確認 AI 可用，回傳 "ok" 或錯誤說明。"""
        try:
            data = self.complete_json("只輸出 JSON。", '請回覆 {"ok": true}', retries=2)
            return "ok" if data.get("ok") else f"回覆異常：{str(data)[:200]}"
        except Exception as exc:
            return f"{type(exc).__name__}：{str(exc)[:400]}"
