# Engine dependency exceptions (P7-T5b, I7)

Audit: `uv export --frozen --all-packages --no-hashes --no-emit-workspace` then `uvx pip-audit -r <export> --no-deps --disable-pip` on 2026-10-02. 26 findings in 7 packages before; fixed ones below were upgraded within the current major in `uv.lock` only (no pyproject range changes).

## Upgraded (lock only)
| Package | From | To | Advisories closed |
|---|---|---|---|
| urllib3 | 2.7.0 | 2.8.0 | PYSEC-2026-4175/4176/4177 |
| anyio | 4.14.1 | 4.15.1 | PYSEC-2026-4023/4024/4025 |
| pyjwt | 2.13.0 | 2.15.1 | PYSEC-2026-4140..4152 except 4146 |
| scrapy | 2.16.0 | 2.17.0 (minimum fix; 2.19 pulls aiohttp and 10 new deps) | PYSEC-2026-3918 |
| cryptography | 49.0.0 | 50.0.2 (major, owner decision H3; pin now >=50,<51; pyopenssl 26.4.0) | PYSEC-2026-3552 |

## Residuals
| Package | Version | Advisory | Reason |
|---|---|---|---|
| pyjwt | 2.15.1 | PYSEC-2026-4146 (CVE-2026-103001) | No fixed version published. Re-audit later. |
| setuptools | 80.10.2 | PYSEC-2026-3447 (CVE-2026-59890) | Fix is 83.0.0 (major); build-time tool, not imported by serving code. Revisit with the build image. |
| pytest | 8.4.2 | PYSEC-2026-1845 (CVE-2025-71176) | Fix is 9.0.3 (major); dev/test only, never shipped. |
