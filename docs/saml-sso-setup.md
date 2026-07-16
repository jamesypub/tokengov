# Configure company SSO (SAML via AWS IAM Identity Center)

> **This guide has moved.** The runtime SAML/SSO setup is now part of
> [**Add tg to your existing AWS IAM Identity Center →
> Ask A**](idc-okta-setup.md#ask-a-register-tgs-admin-console-as-a-saml-app-in-idc),
> where it sits alongside the companion deny-policy step (Ask B) so
> both tg-to-IDC integrations live together.

See [`idc-okta-setup.md`](idc-okta-setup.md) for:

- **Ask A — register tg's admin console as a SAML app in IDC**
  (the SP values, the paste-ready support ticket, applying it in
  Settings → Authentication, verification, break-glass recovery, and
  the request-signing tradeoff) — the content this page used to hold.
- **Ask B — attach the `tg-BedrockQuotaDeny` policy reference to a
  permission set** — the durable IDC-owned enforcement path.
