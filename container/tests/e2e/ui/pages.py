"""UI-tier e2e page objects (Fowler: page objects hold NO assertions —
they model the page and expose actions/queries; the test asserts).
https://martinfowler.com/bliki/PageObject.html

These wrap Playwright locators for the pages the UI-level e2e drives.
Kept minimal to start (the bulk of consistency coverage is the cheaper
API tier); grow as UI e2e cases are added.
"""
from __future__ import annotations


class UsersPage:
    """The Users screen: open the Add-user modal, submit, read the row
    list. No assertions — the test decides pass/fail."""

    def __init__(self, page, base_url: str):
        self.page = page
        self.base_url = base_url.rstrip("/")

    def open(self):
        self.page.goto(f"{self.base_url}/#/users")
        return self

    def open_add_user(self):
        self.page.get_by_role("button", name="+ Add user").click()
        return self

    def add_user(self, email: str):
        self.page.get_by_label("Email").fill(email)
        self.page.get_by_role("button", name="Add user").click()
        return self

    def row_emails(self) -> list[str]:
        """The visible caller/email cells on the Users list."""
        return self.page.locator("[data-user-email]").all_text_contents()
