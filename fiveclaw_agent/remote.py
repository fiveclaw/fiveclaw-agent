"""HTTP client — sends tool calls to the FiveClaw VPS for processing."""

import json
import ssl
import urllib.request
import urllib.error
from typing import Any

# Shared TLS context: enforce TLS 1.2+, cert + hostname verification.
# Created once at module load so it is reused across requests.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.minimum_version = ssl.TLSVersion.TLSv1_2
_SSL_CTX.verify_mode = ssl.CERT_REQUIRED
_SSL_CTX.check_hostname = True


class RemoteClient:
    """Authenticated relay to the FiveClaw VPS intelligence API."""

    def __init__(self, api_key: str, api_url: str):
        if not api_url.startswith("https://"):
            raise ValueError(
                f"FIVECLAW_API_URL must use HTTPS (got: {api_url!r}). "
                "All API calls must be encrypted."
            )
        self.api_key = api_key
        self.api_url = api_url

    def call(self, tool: str, params: dict, files: dict[str, str] | None = None) -> str:
        """
        POST to /api/mcp/tools with the tool name, params, and optional file content.
        Returns the raw JSON string result.
        """
        payload = {
            "tool":   tool,
            "params": params,
            "files":  files or {},
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.api_url}/api/mcp/tools",
            data=data,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent":    "FiveClaw-Agent/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err = json.loads(body)
            except Exception:
                err = {"error": body}

            if e.code == 401:
                return json.dumps({"error": "Invalid or expired API key. Check your FIVECLAW_API_KEY."})
            if e.code == 402:
                return json.dumps({"error": "Subscription required. Visit https://fiveclaw.xyz/pricing"})
            if e.code == 403:
                return json.dumps({"error": err.get("error", "Access denied — your plan may not include this tool.")})
            return json.dumps({"error": f"API error {e.code}: {err.get('error', body[:200])}"})
        except Exception as e:
            return json.dumps({"error": f"Could not reach FiveClaw API: {str(e)}"})
