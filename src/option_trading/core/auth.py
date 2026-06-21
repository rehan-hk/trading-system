"""
Kite Connect auto-login via headless Selenium.

Performs the full browser-based login flow including TOTP and returns
a fresh access token string that can be passed to KiteConnect.
"""
from __future__ import annotations

import time

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)


def autologin(
    api_key: str,
    api_secret: str,
    username: str,
    password: str,
    totp_secret: str,
) -> str:
    """
    Perform a headless Chrome login to Kite Connect and return the access token.

    Args:
        api_key:     Kite API key.
        api_secret:  Kite API secret.
        username:    Kite user ID / username.
        password:    Kite login password.
        totp_secret: Base-32 TOTP secret for 2FA.

    Returns:
        Access token string.

    Raises:
        RuntimeError: If login fails at any stage.
    """
    # Lazy imports — selenium and pyotp are only needed here
    from kiteconnect import KiteConnect
    from pyotp import TOTP
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options

    logger.info("Starting Kite auto-login for user %s", username)

    kite = KiteConnect(api_key=api_key)

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(kite.login_url())
        driver.implicitly_wait(10)

        # Enter username and password
        user_field = driver.find_element(
            By.XPATH,
            '/html/body/div[1]/div/div[2]/div[1]/div/div/div[2]/form/div[1]/input',
        )
        pass_field = driver.find_element(
            By.XPATH,
            '/html/body/div[1]/div/div[2]/div[1]/div/div/div[2]/form/div[2]/input',
        )
        user_field.send_keys(username)
        pass_field.send_keys(password)

        driver.find_element(
            By.XPATH,
            '/html/body/div[1]/div/div[2]/div[1]/div/div/div[2]/form/div[4]/button',
        ).click()

        # Enter TOTP
        pin_field = driver.find_element(
            By.XPATH,
            '/html/body/div[1]/div/div[2]/div[1]/div[2]/div/div[2]/form/div[1]/input',
        )
        totp = TOTP(totp_secret)
        token = totp.now()
        logger.debug("Generated TOTP token")
        pin_field.send_keys(token)

        # Wait for redirect with request_token
        time.sleep(10)

        current_url = driver.current_url
        if "request_token=" not in current_url:
            raise RuntimeError(
                f"Login failed — unexpected redirect URL: {current_url}"
            )

        request_token = current_url.split("request_token=")[1][:32]
        logger.info("Request token obtained: %s…", request_token[:8])

    finally:
        driver.quit()

    # Exchange request token for access token
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token: str = data["access_token"]
    logger.info("Access token obtained successfully.")
    return access_token
