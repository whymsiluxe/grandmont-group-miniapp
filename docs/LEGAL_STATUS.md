# Legal Status Note — Grandmont Group

Recorded 2026-09-27, Wave M. Single source of truth for the current legal
status of the "Grandmont Group" entity referenced throughout the rebrand.

## Current status

**Grandmont Group UG (haftungsbeschränkt) registration is PENDING — NOT yet
confirmed as officially registered.**

This name appears throughout the codebase (website Impressum, CRM-generated
Angebote/Rechnungen, the miniapp's AI system prompt, legal boilerplate) as the
target legal entity for the rebrand. It must never be presented, in any
customer-facing document or public claim, as an already-registered entity
until the owner explicitly confirms registration is complete.

## Where this matters operationally

- **Rechnungen/Angebote** (`miniapp/rechnung.js`, `miniapp/angebot_free.js`):
  currently show `Grandmont Group UG (haftungsbeschränkt)` as the issuing
  entity, but with the OLD entity's real tax numbers (`Steuernummer`,
  `USt-IdNr`, `IBAN` — see `FIRMA` object in `rechnung.js`) sitting next to the
  new name. **This mismatch was flagged during Wave J and deliberately left
  untouched** — those tax/bank details belong to a specific legal entity and
  must not be edited without the owner confirming which entity's numbers are
  actually correct to use going forward. Do not issue real invoices off this
  setup without that confirmation.
- **Website Impressum** (`site-config.ts` / impressum page): already renders
  `legalName` and `legalForm` for Grandmont Group UG (haftungsbeschränkt), with
  a `REGISTRATION_PENDING` comment marker in the source. That marker exists
  specifically so a future pass (or a legal reviewer) can find and re-verify
  this before the site is treated as a real production launch.

## What must NOT happen until registration is confirmed

- No public claim that Grandmont Group UG is a registered entity (website copy,
  marketing material, contracts).
- No real invoices issued under the Grandmont Group name using tax IDs that
  belong to a different legal entity.
- No banking/tax-authority-facing document generated with the new name unless
  the owner has separately confirmed the registration and updated the actual
  tax/bank fields to match.

## What CAN proceed regardless of registration status

- All internal branding, UI text, code identifiers, repo names, domain
  preparation (Wave L) — none of that constitutes a legal claim of
  registration, it's just naming.
- Internal CRM/Mini App usage where the documents aren't yet being sent to
  real customers as final invoices.

## Update this note when

Registration is confirmed (add the confirmation date and any registration
number here, then remove the "PENDING" framing) — or if the legal name/form
changes for any other reason. Don't silently let this note go stale; it's the
one place this status is meant to be checked before anything legal-facing goes
out.