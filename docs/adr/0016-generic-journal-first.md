# ADR 0016: A generic journal export before accounting system formats

Status: accepted

Date: 2026-10-16

## Context

Approved invoices and payments must reach the accounting system. Xero, QuickBooks and Zoho each have their own import format and rules, and no live integration is in scope (hard rule 3: no real money moves).

## Decision

* First a generic journal export (`pairly_pay.journal`): approved invoices, applied credit notes and paid run items of a period as balanced journal entries, in CSV.
* Placeholder control accounts (`AP_CONTROL`, `VAT_INPUT`, `BANK`, `DISCOUNT_RECEIVED`, `SUSPENSE`) that the accounting system maps on import.
* Amounts are decimals at the currency's minor unit; every entry's debits equal its credits exactly, or the export raises.
* When coded lines do not add up to the invoice, the entry falls back to one line on the single coded account or on `SUSPENSE` for a person to reclass. Never dropped, never guessed.
* Xero, QuickBooks and Zoho formats come later, on top of the same entries.

## Consequences

* Any accounting system that imports a journal CSV works today, with a mapping step.
* The later formats are a rendering of entries that are already balanced and tested.
