# ADR 0017: Xero, QuickBooks Online and Zoho Books journal files

Status: accepted

Date: 2026-10-19

## Context

[ADR 0016](0016-generic-journal-first.md) shipped a generic journal export and left the accounting system formats for later, "on top of the same entries". Finance teams in the UAE mostly use Xero, QuickBooks Online or Zoho Books, and each imports manual journals from its own CSV layout. No live integration is in scope.

## Decision

* `POST /v1/pay/journal/export` takes an optional `format`: `generic` (the default, the file is unchanged), `xero`, `quickbooks` or `zoho_books`. The same permission (`pay.journal.export`), the same second factor and the same audit event (`pay.journal.exported`) apply; the format is recorded in `pay_journal_exports.format` and in the audit payload, with the placeholders' account matches and a SHA-256 of all the matches. No new route, no new table.
* The three formats are renderers of the same balanced `JournalLine` entries (`pairly_pay.journal_formats`); nothing is recomputed.
* Accounts: the placeholders (`AP_CONTROL`, `VAT_INPUT`, `BANK`, `DISCOUNT_RECEIVED`, `SUSPENSE`) need a match in the request (`accounts`). GL codes of the chart of accounts in force match themselves for Xero (account codes) and their names for QuickBooks Online and Zoho Books (account names); `accounts` can override any of them.
* Fail closed: the file is refused with 422 and nothing is written or recorded when an entry does not balance, a line's account has no match, or a value does not fit the format (an amount with more decimals than it takes, several currencies in a Xero file, a NUL character). Every text cell goes through the CSV formula guard of the generic export.
* Dates are day first (`dd/mm/yyyy`), the setting of UAE and UK organisations. Amounts have a dot for decimals and no thousands separator.

**Xero.** One row per line: Narration, Date, Description, AccountCode, TaxRate, Amount, TrackingName1, TrackingOption1, TrackingName2, TrackingOption2. Narration and Date are on the first row of each journal; the rows under it leave both blank. Debits are positive, credits negative, at most 2 decimal places, so a KWD or BHD amount with a third decimal is refused. A manual journal has no currency column (it is in the base currency), so a period with more than one currency is refused. TaxRate is the optional `tax_rate` of the request on every line: Pairly posts the VAT on its own line, so pick a rate with no tax. The cost centre goes to the tracking category `Cost Centre`, which must exist. The Xero Central article could not be fetched (it renders in the browser only): the layout was checked against a published copy of the article and the Xero API, so verify it against the template your organisation downloads.

**QuickBooks Online.** JournalNo, JournalDate, Currency, Memo, AccountName, Debits, Credits, Description, Name, Location, Class. The lines of one entry share JournalNo (`PAIRLY-YYYY-MM-<entry>`). Debits and Credits have at most 2 decimal places, the other side blank. Name is the vendor (it must exist in QuickBooks, as the help page asks for Accounts Payable lines), Class the cost centre, Location blank. Sub accounts are written `Parent:Sub` in the account match. The help page names the fields but does not print the sample header or the date format; QuickBooks maps the columns on import.

**Zoho Books.** Journal Date, Journal Number, Reference Number, Notes, Journal Type, Currency, Account, Description, Contact Name, Debit, Credit. Journal Type is `Both` (accrual and cash). Debit and Credit keep the currency's decimal places (3 for KWD, BHD and OMR). The tax columns are left out, since a tax on the expense line would count the VAT twice, and cost centres (reporting tags) are not carried. The help page asks to download the sample file from the import screen and does not list the columns, so verify them against it.

The files were validated against the vendors' published templates and help pages, not against a live vendor account. The source and the date each layout was read are in the renderer's docstring.

## Consequences

* A finance team can import a period into Xero, QuickBooks Online or Zoho Books after matching five accounts, and a file that would import wrong is never made.
* The account matches are typed at each export; storing them in the Pay settings is a later step.
* A layout change by a vendor is a change to one renderer and its golden header test.
