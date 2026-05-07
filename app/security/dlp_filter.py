"""敏感数据脱敏过滤器：用于在返回前屏蔽手机号与身份证号。"""

from __future__ import annotations

import re


class SensitiveDataFilter:
    """统一执行文本脱敏，降低响应中泄露个人敏感信息的风险。"""

    # 11 位手机号（排除前后是数字的场景，避免误匹配更长数字串）
    _PHONE_PATTERN = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
    # 18 位身份证：前 17 位数字，最后一位可能是数字或 X/x
    _ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{17}[0-9Xx])(?![0-9Xx])")
    _EMAIL_PATTERN = re.compile(r"(?i)\b([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,})\b")
    # 常见银行卡号长度 16-19 位，允许中间有空格或横杠。
    _BANK_CARD_PATTERN = re.compile(r"(?<!\d)(\d[ -]?){16,19}(?!\d)")

    @classmethod
    def mask_sensitive_data(cls, text: str) -> str:
        """按规则对输入文本做脱敏。

        规则 1：手机号 `13812345678` -> `138****5678`。
        规则 2：18 位身份证 `110101199001011234` -> `1101**********1234`。
        规则 3：邮箱 `user@example.com` -> `u***@example.com`。
        规则 4：银行卡号仅保留前 4 位与后 4 位。

        Args:
            text: 待脱敏的原始文本。

        Returns:
            脱敏后的安全文本。
        """
        if not text:
            return text

        masked = cls._PHONE_PATTERN.sub(lambda m: f"{m.group(1)[:3]}****{m.group(1)[-4:]}", text)
        masked = cls._ID_CARD_PATTERN.sub(
            lambda m: f"{m.group(1)[:4]}{'*' * 10}{m.group(1)[-4:]}",
            masked,
        )
        masked = cls._EMAIL_PATTERN.sub(lambda m: f"{m.group(1)[:1]}***@{m.group(2)}", masked)
        masked = cls._BANK_CARD_PATTERN.sub(cls._mask_bank_card, masked)
        return masked

    @staticmethod
    def _mask_bank_card(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 16:
            return raw
        return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"
