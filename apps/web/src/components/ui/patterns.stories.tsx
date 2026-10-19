// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { Inbox } from "lucide-react";

import { EmptyState } from "@/components/states/empty-state";

import { Badge } from "./badge";
import { Button } from "./button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  dialogTitleClass,
  SheetContent,
} from "./dialog";
import { Evidence } from "./evidence";
import { FilterBar, FilterField, filterApplyClass, filterFieldClass } from "./filter-bar";
import { Kbd } from "./kbd";
import { Section } from "./section";
import { TabNav, tabClass } from "./tab-nav";
import { Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow } from "./table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./tabs";

const ROWS = [
  { id: "INV-2041", vendor: "Gulf Office Supply", due: "2026-10-22", amount: "12,480.00", status: "warning" as const },
  { id: "INV-2042", vendor: "Al Noor Logistics", due: "2026-10-25", amount: "3,150.50", status: "success" as const },
  { id: "INV-2043", vendor: "Sahara Print House", due: "2026-10-19", amount: "870.00", status: "danger" as const },
];
const LABEL = { warning: "Due soon", success: "Approved", danger: "Overdue" };

/**
 * The wave 2 patterns: the shared table, filter bars, evidence quotes, tabs, section
 * cards, the dialog and sheet layout and empty states. Every screen builds on these.
 */
function Patterns() {
  return (
    <div className="space-y-10 p-8">
      <section className="space-y-3">
        <p className="eyebrow">Filter bar</p>
        <FilterBar aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
          <FilterField label="Job">
            <select className={filterFieldClass} defaultValue="">
              <option value="">All jobs</option>
              <option>Senior accountant</option>
            </select>
          </FilterField>
          <FilterField label="Since">
            <input type="date" className={filterFieldClass} defaultValue="2026-09-01" />
          </FilterField>
          <button type="submit" className={filterApplyClass}>
            Apply
          </button>
        </FilterBar>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Table</p>
        <Table maxHeight="16rem" label="Invoices">
          <caption className="sr-only">Invoices due this week</caption>
          <TableHead>
            <TableRow>
              <TableHeaderCell>Invoice</TableHeaderCell>
              <TableHeaderCell>Vendor</TableHeaderCell>
              <TableHeaderCell>Due</TableHeaderCell>
              <TableHeaderCell numeric>Amount (AED)</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {ROWS.map((row) => (
              <TableRow key={row.id}>
                <TableCell className="font-mono text-xs">{row.id}</TableCell>
                <TableCell>{row.vendor}</TableCell>
                <TableCell className="font-mono text-xs">{row.due}</TableCell>
                <TableCell numeric>{row.amount}</TableCell>
                <TableCell>
                  <Badge variant={row.status}>{LABEL[row.status]}</Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </section>

      <section className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-3">
          <p className="eyebrow">Evidence</p>
          <Evidence
            quote="Led month-end close for a 40-entity group, reconciling intercompany balances in IFRS 16."
            highlight={["month-end close", "IFRS 16"]}
            source="CV, page 1"
            sourceHref="#cv"
            meta="Matched 2 of 3 terms"
          />
        </div>
        <Section title="Criteria" actions={<Badge variant="info">3 of 4 met</Badge>} description="A section card: hairline, no shadow.">
          <ul className="space-y-1 text-sm">
            <li>Five years in accounting</li>
            <li>IFRS reporting</li>
          </ul>
        </Section>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Tabs</p>
        <TabNav aria-label="Job sections">
          <a href="#overview" aria-current="page" className={tabClass(true)}>
            Overview
          </a>
          <a href="#pipeline" className={tabClass(false)}>
            Pipeline
          </a>
          <a href="#screen" className={tabClass(false)}>
            Screen
          </a>
        </TabNav>
        <Tabs defaultValue="notes">
          <TabsList aria-label="Candidate">
            <TabsTrigger value="notes">Notes</TabsTrigger>
            <TabsTrigger value="timeline">Timeline</TabsTrigger>
          </TabsList>
          <TabsContent value="notes" className="text-sm text-muted-foreground">
            Panels inside one page move with the arrow keys.
          </TabsContent>
          <TabsContent value="timeline" className="text-sm text-muted-foreground">
            Applied, screened, interviewed.
          </TabsContent>
        </Tabs>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Keyboard hints</p>
        <div className="flex flex-wrap gap-3 text-sm text-muted-foreground">
          {[
            ["J", "Next"],
            ["K", "Previous"],
            ["A", "Advance"],
            ["H", "Hold"],
            ["R", "Reject"],
          ].map(([key, label]) => (
            <span key={key} className="flex items-center gap-1.5">
              <Kbd>{key}</Kbd>
              {label}
            </span>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Empty state</p>
        <div className="card-hairline px-5">
          <EmptyState icon={<Inbox aria-hidden />} title="Nothing waiting for you" body="New approvals appear here as they arrive." />
        </div>
      </section>
    </div>
  );
}

/** A dialog with the shared header, body and footer, shown open. */
function DialogLayout() {
  return (
    <Dialog open>
      <DialogContent className="overflow-hidden">
        <DialogHeader>
          <DialogTitle className={dialogTitleClass}>Reject this invoice?</DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">The vendor sees the reason you give.</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <textarea aria-label="Reason" className="min-h-24 w-full rounded-md border border-input bg-surface p-3 text-sm" />
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost">Cancel</Button>
          <Button variant="destructive">Reject</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** A sheet from the end side, for details that keep the list in view. */
function SheetLayout() {
  return (
    <Dialog open>
      <SheetContent>
        <DialogHeader>
          <p className="eyebrow">Candidate</p>
          <DialogTitle className={dialogTitleClass}>Candidate 4</DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">Applied 3 days ago</DialogDescription>
        </DialogHeader>
        <DialogBody className="flex-1">
          <Evidence quote="Prepared consolidated IFRS statements for 12 entities." highlight="IFRS" source="CV, page 2" sourceHref="#cv" />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline">Hold</Button>
          <Button>Advance</Button>
        </DialogFooter>
      </SheetContent>
    </Dialog>
  );
}

const meta = { title: "UI/Patterns", component: Patterns } satisfies Meta<typeof Patterns>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Light: Story = {};
export const Dark: Story = { globals: { theme: "dark" } };
export const Arabic: Story = { globals: { locale: "ar" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
export const DialogOpen: Story = { render: () => <DialogLayout /> };
export const SheetOpen: Story = { render: () => <SheetLayout /> };
export const SheetOpenArabic: Story = { render: () => <SheetLayout />, globals: { locale: "ar" } };
