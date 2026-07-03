import asyncio
import os
import ssl

import aiohttp
from roborock.web_api import RoborockApiClient

try:
    import certifi
except ModuleNotFoundError:
    certifi = None

# Usage:
#   export ROBOROCK_USERNAME='email'
#   export ROBOROCK_PASSWORD='password'
# Optional (only for troubleshooting):
#   export ROBOROCK_INSECURE_SSL=1

USERNAME = os.environ.get("ROBOROCK_USERNAME", "").strip()
PASSWORD = os.environ.get("ROBOROCK_PASSWORD", "").strip()
INSECURE_SSL = os.environ.get("ROBOROCK_INSECURE_SSL", "0") in {"1", "true", "TRUE", "yes", "YES"}


def build_ssl_context() -> ssl.SSLContext:
    if INSECURE_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # Use certifi when available, otherwise fallback to system trust store.
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


async def main() -> None:
    if not USERNAME or not PASSWORD:
        raise SystemExit(
            "Missing credentials. Set ROBOROCK_USERNAME and ROBOROCK_PASSWORD environment variables."
        )

    ssl_context = build_ssl_context()

    # Ensure aiohttp uses our SSL context.
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        web_api = RoborockApiClient(username=USERNAME, session=session)

        print("Connexion au cloud Roborock…")
        user_data = await web_api.pass_login(password=PASSWORD)
        print("Connecté au cloud Roborock")

        result = await web_api.get_home_data_v3(user_data)
        print("Home data:", result)


if __name__ == "__main__":
    asyncio.run(main())
