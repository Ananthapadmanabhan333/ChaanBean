# Supabase authentication

Supabase is wired in as the **identity provider**. Switching to it is two
settings and no code change.

```bash
AUTH_BACKEND=supabase
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<anon key>
# Legacy projects only — newer ones sign asymmetrically and use JWKS:
# SUPABASE_JWT_SECRET=<HS256 secret>
```

---

## What it replaces, and what it explicitly does not

| Supabase now owns | This system still owns |
|---|---|
| Signup, login, password reset | **Which company a user belongs to** |
| OTP, magic links, social, MFA | **What they may do** (`owner`, `admin`, `operator`, `viewer`, `legal_approver`) |
| Session and refresh tokens | Row-Level Security, bound to `app.company_id` |
| The password itself | The ledger, policy engine, campaigns, everything else |

Supabase is not the database. Your PostgreSQL, its RLS policies and the whole
domain stay exactly where they are. Supabase answers one question — *who is this
person* — and this system answers the rest.

## The security decision that shapes this

A Supabase JWT carries `user_metadata`, and **`user_metadata` is writable by the
user** through the client SDK. So a token asserting:

```json
{ "user_metadata": { "role": "legal_approver", "company_id": "..." } }
```

proves nothing at all. If authorization read from there, anyone who could sign up
to your Supabase project could approve L3 legal content and point themselves at
another company's debtors.

So `app/identity/supabase.py` takes exactly **two** facts from a verified token —
`sub` and `email` — and looks up everything else in our `users` and `roles`
tables. `app_metadata` is admin-only and safer, but it is not read either: it
still lives in a system whose job is authentication, not tenancy.

`tests/test_supabase_auth.py` asserts this directly. Wiring roles back to
`user_metadata` makes the suite fail with `legal_approver` and `owner` appearing
on a user who has neither — verified, not assumed.

## How verification works

* **Signature always checked.** No path decodes without verification.
* **Asymmetric first.** Newer projects publish public keys at
  `/auth/v1/.well-known/jwks.json`; those are fetched and cached for 10 minutes,
  so key rotation is picked up without a network call per request. Legacy
  projects fall back to the shared HS256 secret.
* **Audience pinned** to `authenticated`, so a service-role or anon key cannot
  pass as an end user.
* **`exp` and `sub` required.**

## Onboarding a user

Authenticated is not the same as authorised. A valid Supabase token for someone
with no row here gets **403**, not a new tenant — otherwise anyone who signed up
to the project would land inside a company.

The intended flow:

1. An admin creates the user in this system with their email and roles.
2. The person signs up or is invited in Supabase with that same email.
3. On first sign-in the Supabase `sub` is bound to their row, and every login
   after that matches on the stable id rather than the email.

Users created before this can be linked in bulk:

```sql
UPDATE users u SET external_auth_id = a.id
FROM auth_users_export a WHERE lower(a.email) = lower(u.email);
```

## What the browser does

In `supabase` mode the password never reaches this backend. The sign-in page
posts directly to `https://<project>.supabase.co/auth/v1/token`, gets an access
token, and sends it as a bearer token to this API — which verifies it by
signature. `GET /api/auth/config` tells the page which mode is active, so the
same page serves both.

To add magic links, Google sign-in or MFA, use the Supabase JS client on the
sign-in page instead of the password call. Nothing on the API side changes: it
verifies whatever access token Supabase issued.

## Going back

`AUTH_BACKEND=local` restores bcrypt and locally-issued JWTs. Both code paths
stay tested, so this is a real fallback rather than a theoretical one — which
matters, because an identity provider is a hard dependency to be stuck behind
during an incident.

## Not done

* **Supabase Row-Level Security.** Ours is enforced by our own PostgreSQL
  against `app.company_id`. If you ever move the data into Supabase's database,
  the policies have to be rewritten against `auth.uid()` — that is a migration,
  not a settings change.
* **Automatic user provisioning.** Deliberate; see above.
* **Refresh-token rotation in the portal.** Supabase access tokens last an hour
  and the page currently asks the user to sign in again. Wiring
  `supabase.auth.onAuthStateChange` would fix it and is a small piece of work.
