"""SMS provider interface + a mock default and real-shaped stubs.

Why mock is the default
-----------------------
Deployed in mainland China. Individuals cannot yet register an SMS 签名 (sign)
or 模板 (template) with Aliyun/Tencent, so real sending is impossible right now.
``MockSMS`` is therefore the default; Aliyun/Tencent live behind the same
interface as stubs that document the request shape but make NO network call.

COST CONTROL: nothing in this module performs any network / API call.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

from . import config

logger = logging.getLogger("videosummary.account.sms")


class NotConfiguredError(RuntimeError):
    """Raised when a real provider is selected but its credentials are absent."""


class SMSProvider(ABC):
    """Send a one-time code to a phone number. Implementations must never log
    the full phone number in production paths."""

    @abstractmethod
    def send(self, phone: str, code: str) -> None:  # pragma: no cover - abstract
        ...


class MockSMS(SMSProvider):
    """No-op provider for dev/test.

    Logs ``OTP for {last4}={code}`` (last4 only — never the full number) and
    stashes the last code per-phone in an in-memory dict so tests can assert on
    it without parsing logs.
    """

    def __init__(self) -> None:
        # phone -> last code. In-process only; fine for a single test/dev box.
        self.last_codes: Dict[str, str] = {}

    def send(self, phone: str, code: str) -> None:
        last4 = phone[-4:] if phone else "????"
        self.last_codes[phone] = code
        logger.info("OTP for %s=%s", last4, code)


class AliyunSMS(SMSProvider):
    """Real-shaped Aliyun Dysmsapi stub. Does NOT hit the network.

    Reads credentials from the environment:
        AUTH_ALIYUN_ACCESS_KEY_ID
        AUTH_ALIYUN_ACCESS_KEY_SECRET
        AUTH_ALIYUN_SIGN_NAME      (短信签名)
        AUTH_ALIYUN_TEMPLATE_CODE  (短信模板 CODE, e.g. SMS_123456789)
    """

    def __init__(self) -> None:
        self.access_key_id = os.environ.get("AUTH_ALIYUN_ACCESS_KEY_ID")
        self.access_key_secret = os.environ.get("AUTH_ALIYUN_ACCESS_KEY_SECRET")
        self.sign_name = os.environ.get("AUTH_ALIYUN_SIGN_NAME")
        self.template_code = os.environ.get("AUTH_ALIYUN_TEMPLATE_CODE")
        missing = [
            name
            for name, val in (
                ("AUTH_ALIYUN_ACCESS_KEY_ID", self.access_key_id),
                ("AUTH_ALIYUN_ACCESS_KEY_SECRET", self.access_key_secret),
                ("AUTH_ALIYUN_SIGN_NAME", self.sign_name),
                ("AUTH_ALIYUN_TEMPLATE_CODE", self.template_code),
            )
            if not val
        ]
        if missing:
            raise NotConfiguredError(
                "AliyunSMS not configured; missing env: " + ", ".join(missing)
            )

    def send(self, phone: str, code: str) -> None:
        # Request shape for Aliyun Dysmsapi ``SendSms`` (RPC, API version
        # 2017-05-25), endpoint dysmsapi.aliyuncs.com:
        #
        #   params = {
        #       "Action": "SendSms",
        #       "Version": "2017-05-25",
        #       "RegionId": "cn-hangzhou",
        #       "PhoneNumbers": phone,                # E.164 without +86, e.g. 13800138000
        #       "SignName": self.sign_name,           # 已审核签名
        #       "TemplateCode": self.template_code,   # 已审核模板, e.g. SMS_123456789
        #       "TemplateParam": json.dumps({"code": code}),
        #       # + RPC common params: Format=JSON, AccessKeyId, SignatureMethod=HMAC-SHA1,
        #       #   SignatureVersion=1.0, SignatureNonce, Timestamp (ISO8601 UTC), Signature
        #   }
        # The Signature is HMAC-SHA1 over the percent-encoded, sorted query
        # string with key ``self.access_key_secret + "&"`` (Aliyun RPC v1 style).
        #
        # TODO(real-sms): perform the HTTP GET/POST to dysmsapi.aliyuncs.com here
        # once an SMS 签名/模板 is approved. Intentionally NOT implemented to keep
        # this package network-free and cost-free.
        raise NotConfiguredError(
            "AliyunSMS.send is a stub: real network sending is disabled (no "
            "approved 签名/模板 yet). Use AUTH_SMS_PROVIDER=mock."
        )


class TencentSMS(SMSProvider):
    """Real-shaped Tencent Cloud SMS stub. Does NOT hit the network.

    Reads credentials from the environment:
        AUTH_TENCENT_SECRET_ID
        AUTH_TENCENT_SECRET_KEY
        AUTH_TENCENT_SMS_SDK_APP_ID  (短信应用 SdkAppId)
        AUTH_TENCENT_SIGN_NAME       (短信签名)
        AUTH_TENCENT_TEMPLATE_ID     (短信模板 ID)
    """

    def __init__(self) -> None:
        self.secret_id = os.environ.get("AUTH_TENCENT_SECRET_ID")
        self.secret_key = os.environ.get("AUTH_TENCENT_SECRET_KEY")
        self.sdk_app_id = os.environ.get("AUTH_TENCENT_SMS_SDK_APP_ID")
        self.sign_name = os.environ.get("AUTH_TENCENT_SIGN_NAME")
        self.template_id = os.environ.get("AUTH_TENCENT_TEMPLATE_ID")
        missing = [
            name
            for name, val in (
                ("AUTH_TENCENT_SECRET_ID", self.secret_id),
                ("AUTH_TENCENT_SECRET_KEY", self.secret_key),
                ("AUTH_TENCENT_SMS_SDK_APP_ID", self.sdk_app_id),
                ("AUTH_TENCENT_SIGN_NAME", self.sign_name),
                ("AUTH_TENCENT_TEMPLATE_ID", self.template_id),
            )
            if not val
        ]
        if missing:
            raise NotConfiguredError(
                "TencentSMS not configured; missing env: " + ", ".join(missing)
            )

    def send(self, phone: str, code: str) -> None:
        # Request shape for Tencent Cloud SMS ``SendSms`` (sms.tencentcloudapi.com,
        # API version 2021-01-11, TC3-HMAC-SHA256 signature):
        #
        #   body = {
        #       "PhoneNumberSet": ["+86" + phone],     # E.164, +86 prefix required
        #       "SmsSdkAppId": self.sdk_app_id,
        #       "SignName": self.sign_name,            # 已审核签名
        #       "TemplateId": self.template_id,        # 已审核模板
        #       "TemplateParamSet": [code],            # ordered template params
        #   }
        #   headers = {
        #       "Content-Type": "application/json; charset=utf-8",
        #       "Host": "sms.tencentcloudapi.com",
        #       "X-TC-Action": "SendSms",
        #       "X-TC-Version": "2021-01-11",
        #       "X-TC-Timestamp": str(int(time.time())),
        #       "X-TC-Region": "ap-guangzhou",
        #       "Authorization": "<TC3-HMAC-SHA256 ... Signature=...>",
        #   }
        #
        # TODO(real-sms): perform the HTTPS POST to sms.tencentcloudapi.com here
        # once an SMS 签名/模板 is approved. Intentionally NOT implemented to keep
        # this package network-free and cost-free.
        raise NotConfiguredError(
            "TencentSMS.send is a stub: real network sending is disabled (no "
            "approved 签名/模板 yet). Use AUTH_SMS_PROVIDER=mock."
        )


# A single shared MockSMS instance so its ``last_codes`` dict survives across
# calls within a process (tests rely on this).
_MOCK_SINGLETON: Optional[MockSMS] = None


def get_provider(provider: Optional[str] = None) -> SMSProvider:
    """Factory keyed on ``AUTH_SMS_PROVIDER`` (or the explicit ``provider``)."""
    name = (provider or config.SETTINGS.sms_provider or "mock").strip().lower()
    if name == "mock":
        global _MOCK_SINGLETON
        if _MOCK_SINGLETON is None:
            _MOCK_SINGLETON = MockSMS()
        return _MOCK_SINGLETON
    if name == "aliyun":
        return AliyunSMS()
    if name == "tencent":
        return TencentSMS()
    raise NotConfiguredError(f"Unknown SMS provider: {name!r}")
