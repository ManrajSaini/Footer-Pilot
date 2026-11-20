# FooterPilot — Later (V2 and beyond)

Not part of V1. Do not build any of this until `plan.md` is complete and V1 has paying customers.

## From idea.md (V2)

- **Microsoft 365 / Outlook support**: same product for Outlook on desktop, web and mobile.
- **Marketplace install**: Google Workspace Marketplace and Microsoft marketplace listings, including the required OAuth verification and security assessment.
- **Department and role-based signatures**: multiple signatures per organisation, assigned by department, office, country, Google group or org unit.
- **Scheduled and targeted campaigns**: banners with start and end dates, different banners per department or region.
- **Banner A/B testing**: two variants, compare click rates.
- **Advanced analytics**: clicks by department, campaign and period, with CSV export.
- **Legal disclaimers**: automatic notices that vary by country or department.
- **Employee self-edit with approval**: employees update allowed fields (pronouns, booking link, badges); brand elements stay locked.
- **Multiple admins and roles**: IT and marketing permissions, plus a change history (audit log).
- **MSP partner console**: one dashboard for many client organisations, partner pricing and reseller margin.
- **Integrations**: HR and CRM tools as data sources.
- **Security certification**: SOC 2 or ISO 27001, prepared answers for security questionnaires.

## Deferred from V1 by the plan

- **Drag-and-drop designer**: reorderable blocks first, free-form canvas after. Likely needs a richer frontend than HTMX.
- **More templates** and brand-coloured social icons.
- **Alias support**: signatures for secondary "send as" addresses, and a shorter reply signature.
- **Real-time directory updates**: Google push notifications instead of hourly polling.
- **Email notifications**: deployment failures, lost connection, over-limit warnings, weekly click summary.
- **Per-employee click reports**: the data is already collected (`click_events.employee_id`); only the UI is missing.
- **Better bot filtering** for click counts, and an optional custom tracking domain per customer.
- **Image uploads in the public generator**, plus saved signatures behind a free account.
- **Offboarding options**: remove signatures from all mailboxes on disconnect or for departed employees.
- **Billing extras**: annual plans, multiple currencies, a merchant-of-record provider for global tax, invoices in-app.
- **Internal admin panel** for support (view organisations, re-run sync, impersonate).
- **Localisation** of the app and of signature field labels.

## Signature placement in replies (found in Phase 5 testing)

Gmail puts an API-set signature below the earlier messages in replies and forwards unless each person
ticks one box in Gmail settings, and Google exposes no way to set it for them. V1
shows employees how. Two ways other products remove the per-person step:

- **Browser extension**: inserts the signature in the compose window, under the reply.
  Admins can force-install it on managed Chrome. Desktop Chrome only; needs building,
  store review and upkeep. Fits the "we never touch your email" promise.
- **Server-side mode**: the customer adds a Google Workspace routing rule so outgoing
  mail passes through our servers, which add the signature after sending. Works on every
  device including mobile. Conflicts with the current privacy promise and the V1
  out-of-scope list (mail would pass through our systems), and makes us responsible for
  mail delivery. Only worth considering as an opt-in for larger customers.

## Growth work (non-code or mostly content)

- Comparison pages against existing signature tools.
- More SEO landing pages built on the generator (per email client, per profession).
- Review-site listings (G2, Capterra) and a prompt asking happy customers for reviews.
