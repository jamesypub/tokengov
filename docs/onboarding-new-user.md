# Pilot dev onboarding — day-one setup

Nothing changes about how you work. You keep your existing AWS
sign-in and run Claude Code exactly as you do today — Token
Governance just adds a per-user spend cap behind the scenes and a
web page where you can see your own usage.

## Run Claude Code — no change

Sign in the way you already do (your existing SSO/AWS login), then
run `claude` as usual. Your Bedrock usage is attributed to you and
counted toward your monthly dollar cap automatically. No new
profiles, keys, or extra auth steps.

## See your spend — the developer web view

Your admin sends you the web URL and enables your login. Sign in and
you land on **your own page**:

- **Spend (month-to-date)** against your cap
- A **profile** section where you set your display name and link your
  GitHub username (which ties your PRs to your usage for velocity
  reporting)

You see only your own data — no org-wide or other-user views.

## When you hit your cap

Bedrock returns `AccessDeniedException` mentioning
`tg-BedrockQuotaDeny` — your monthly spend cap is reached. Ask your
admin to raise it, or wait for the next month's reset (1st, UTC).

## Questions

Ping your admin. They can raise your cap or check the details of any
error.
