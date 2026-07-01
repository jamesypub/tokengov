# Federate Okta into the Cognito user pool

The `tg-cognito-pool` stack provisions a Cognito User Pool that
serves as the OIDC IdP for the tg-admin SPA. Out of the box the
App Client only advertises `COGNITO`, so the only login mode is
username + password. This doc covers wiring **Okta as a SAML
provider** so the same Hosted UI also offers "Sign in with Okta".

The two paths are **additive** — turning Okta on does not break
the username/password path. The bootstrap admin and any other
Cognito-native users keep working.

---

## What you create on the Okta side

(Done by your Okta admin — Claude can't do this for you.)

1. **Create a SAML 2.0 application** in your Okta tenant.
   - Sign-on method: SAML 2.0
   - App name: `tg-admin` (or whatever you prefer)
2. **Configure the SAML settings** with values Cognito expects:
   - **Single sign-on URL:**
     `https://<HostedUiDomain>/saml2/idpresponse`
     (e.g. `https://tg-admin-<account>.auth.us-east-1.amazoncognito.com/saml2/idpresponse`)
   - **Audience URI (SP Entity ID):**
     `urn:amazon:cognito:sp:<UserPoolId>`
     (e.g. `urn:amazon:cognito:sp:us-east-1_AbCdEf123`)
   - **Name ID format:** EmailAddress
   - **Application username:** Email
3. **Attribute statements** — Cognito will read the `email`
   claim out of the SAML assertion. On the SAML app's **Sign On**
   tab → "SAML Settings" → "Edit" → "Attribute Statements", add:
   ```
   Name:       http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress
   Name format: Unspecified
   Value:      user.profile.email
   ```
   If your tenant accepts `user.email` (the shorthand), that
   works too — but on stripped-down tenants (Integrator Free,
   etc.) the shorthand can fail with "Invalid property email
   in expression user.email". The explicit-path form
   `user.profile.email` is more portable. Verify either by
   clicking **"Preview the SAML Assertion"** — the XML must
   contain `<saml2:AttributeValue>your-real-email@…</saml2:AttributeValue>`,
   not the literal text `user.email`.

   If you map under a different attribute name, pass that name
   as the `OktaEmailAttribute` CFN param.
4. **Assign users / groups** to the app — open the
   **Assignments** tab on the app, click **Assign** → "Assign
   to People" (or "Assign to Groups"), and add yourself plus
   any pilot users. **This step is mandatory** — an
   unassigned user clicking "Sign in with Okta" gets a
   generic Okta error and never reaches Cognito. Forgetting
   this is the #1 cause of "the button is there but login
   fails".
5. **Verify the assertion before handing off.** On the
   **Sign On** tab click **"Preview the SAML Assertion"**.
   The XML must show your real email under the
   `emailaddress` attribute, e.g.:
   ```xml
   <saml2:AttributeValue ...>you@example.com</saml2:AttributeValue>
   ```
   If it shows the literal text `user.email` or
   `user.profile.email`, the expression got saved as a
   string literal (quoted) — fix it before sending the
   metadata URL.
6. **Grab the metadata URL** from Okta's app page (often labeled
   "Identity Provider metadata"). It looks like:
   ```
   https://<your-tenant>.okta.com/app/<okta-app-id>/sso/saml/metadata
   ```

Send that metadata URL back to the install side.

---

## What you do on the AWS side

Re-deploy `tg-cognito-pool` with the metadata URL set:

```
aws cloudformation deploy \
  --profile tg-install-stage-<account> \
  --template-file cfn/tg-cognito-pool.yaml \
  --stack-name tg-cognito-pool \
  --parameter-overrides \
    BootstrapAdminEmail=<your-email> \
    CallbackUrl=https://<alb-dns>/auth/callback \
    OktaMetadataUrl=https://<tenant>.okta.com/app/<app-id>/sso/saml/metadata \
    OktaProviderName=Okta
```

Verify:

```
aws --profile tg-install-stage-<account> \
  cognito-idp list-identity-providers \
  --user-pool-id <UserPoolId>
# expect: Providers contains {ProviderName: Okta, ProviderType: SAML}

aws --profile tg-install-stage-<account> \
  cognito-idp describe-user-pool-client \
  --user-pool-id <UserPoolId> \
  --client-id <ClientId> \
  --query UserPoolClient.SupportedIdentityProviders
# expect: ["COGNITO", "Okta"]
```

The `tg-api` task does **not** need a redeploy — `/auth/login`
already passes `?identity_provider=` through to Cognito's
authorize endpoint as of #192.

---

## Login surface

After the redeploy, three URLs all work:

| URL | Behaviour |
|---|---|
| `https://<alb>/auth/login` | Hosted UI picker — user chooses Okta or username/password |
| `https://<alb>/auth/login?identity_provider=Okta` | Skip picker, go straight to Okta SSO |
| `https://<alb>/auth/login?identity_provider=COGNITO` | Skip picker, go straight to user/pw form |

All admin access is through this web login (the Hosted UI →
the SPA); there is no separate admin client.

---

## Troubleshooting

- **Hosted UI shows Okta button but the click 400s.** The Okta
  app's "Single sign-on URL" likely doesn't match the Hosted
  UI domain exactly. Use the value Cognito reports in
  `describe-user-pool-domain` output, not the value you typed
  into the CFN param.
- **Login succeeds in Okta but `/auth/callback` returns
  "id_token has no email claim".** Okta is sending email under
  a different attribute name. Re-deploy with
  `OktaEmailAttribute=<the actual name>`.
- **Login succeeds but the SPA shows "no admin role for
  <email>".** The federated user landed in `admin_roles` keyed
  on the email Okta asserted, but no one's granted them a role
  yet. POST to `/api/admin-roles` as an existing org_admin
  (signed in through the web login).

---

## Test plan

After the Okta redeploy:

- [ ] `curl -v https://<alb>/auth/login` shows the Hosted UI
      with both Okta and username/password buttons
- [ ] Logging in via Okta lands at the SPA with the
      org_admin's email in the top-right
- [ ] Logging in via username/password (existing path) still
      works
- [ ] An authenticated admin can call `/api/admin-roles`
      (returns the role list)

---

## Branded login surfaces (#193)

Two pages carry the "Cost vs. Value / Start with cost. End
with outcome." treatment:

1. **`/login`** (the SPA's pre-auth landing card) — fully
   under our control; eyebrow `TOKEN GOVERNANCE · tg-admin`,
   AWS-orange CTA, dark surface. Anonymous browser hits to
   `/`, `/users`, `/docs`, etc. 302 here first (auth-gate),
   not directly to Cognito.

2. **Cognito Hosted UI** (`/auth/login` → Cognito domain) —
   `AWS::Cognito::ManagedLoginBranding` (`UseCognitoProvidedValues:
   true`) gives the Hosted UI its branded chrome. Cognito's
   API does **not** allow custom page text/copy — only colors,
   logos, and fonts — so the "Cost vs. Value" header lives
   on `/login`, not on the Hosted UI.

The pool's `UserPoolTier: ESSENTIALS` + `UserPoolDomain.ManagedLoginVersion:
2` are required for the branded chrome; LITE-tier pools
only get the classic unbrandable Hosted UI. `tg-cognito-pool.yaml`
sets both.

`DesktopAuthScreen.jsx` shares the same eyebrow / H1 / lede
pattern so its pre-SSO landing card matches the cloud `/login`
surface.

---

## Related

- `cfn/tg-cognito-pool.yaml` — pool template
- `container/api/auth_routes.py` — `/auth/login` + callback
- `container/api/auth_gate.py` — anonymous → `/login` redirect
- `admin-ui/web/src/pages/Login.jsx` — branded landing card
- `admin-ui/web/src/pages/DesktopAuthScreen.jsx` — desktop
  pre-SSO card (shares the visual pattern)
- `container/api/oidc.py` — `authorize_url(... identity_provider=)`
