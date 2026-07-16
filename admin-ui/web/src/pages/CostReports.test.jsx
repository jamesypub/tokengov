import { describe, it, expect } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, within } from '@testing-library/react'
import {
  isNumericCell, compareCells, sortRows, ResultTable,
  rangeState, rangeLabel,
  monthStartISO, todayISO, formatCell, headerLabel,
} from './CostReports'

// #830: client-side, type-aware column sort on the shared
// ResultTable. The load-bearing detail is the comparator — a naive
// string sort puts "$1,041.86" below "$581.70". These pin the
// comparator, the pure sortRows helper, and the rendered header
// affordance (aria-sort + keyboard).

describe('isNumericCell (#830)', () => {
  it('treats currency / number / percent as numeric', () => {
    expect(isNumericCell('$1,041.86')).toBe(true)
    expect(isNumericCell('42')).toBe(true)
    expect(isNumericCell('3.5%')).toBe(true)
    expect(isNumericCell('-12.00')).toBe(true)
  })
  it('treats text and blanks as non-numeric', () => {
    expect(isNumericCell('alice@test.com')).toBe(false)
    expect(isNumericCell('us-east-1')).toBe(false)
    expect(isNumericCell('')).toBe(false)
    expect(isNumericCell('   ')).toBe(false)
    expect(isNumericCell(null)).toBe(false)
  })
})

describe('compareCells (#830)', () => {
  it('compares currency numerically, not lexically', () => {
    // The bug this guards: "$1,041.86" < "$581.70" as strings ('1'<'5').
    expect(compareCells('$1,041.86', '$581.70')).toBeGreaterThan(0)
    expect(compareCells('$581.70', '$1,041.86')).toBeLessThan(0)
  })
  it('compares dates chronologically', () => {
    expect(compareCells('2026-06-01', '2026-06-09')).toBeLessThan(0)
    expect(compareCells('2026-06-09 12:00:00', '2026-06-09 09:00:00'))
      .toBeGreaterThan(0)
  })
  it('compares text case-insensitively', () => {
    expect(compareCells('alice', 'Bob')).toBeLessThan(0)
    expect(compareCells('Zed', 'apple')).toBeGreaterThan(0)
  })
  it('sorts blanks last regardless of type', () => {
    expect(compareCells('', '5')).toBeGreaterThan(0)
    expect(compareCells('5', '')).toBeLessThan(0)
    expect(compareCells('', '')).toBe(0)
  })
})

describe('sortRows (#830)', () => {
  const rows = [
    ['alice@test.com', '$581.70',   '2026-06-02'],
    ['bob@test.com',   '$1,041.86', '2026-06-01'],
    ['carol@test.com', '$42.00',    '2026-06-09'],
  ]

  it('col=null returns the rows unchanged (server order)', () => {
    expect(sortRows(rows, null, 'asc')).toBe(rows)
  })

  it('sorts a currency column descending by numeric value', () => {
    const out = sortRows(rows, 1, 'desc')
    expect(out.map(r => r[0])).toEqual([
      'bob@test.com',    // $1,041.86
      'alice@test.com',  // $581.70
      'carol@test.com',  // $42.00
    ])
  })

  it('sorts a currency column ascending by numeric value', () => {
    const out = sortRows(rows, 1, 'asc')
    expect(out.map(r => r[1])).toEqual(['$42.00', '$581.70', '$1,041.86'])
  })

  it('sorts a date column chronologically', () => {
    const out = sortRows(rows, 2, 'asc')
    expect(out.map(r => r[2])).toEqual([
      '2026-06-01', '2026-06-02', '2026-06-09',
    ])
  })

  it('does not mutate the input array', () => {
    const before = rows.map(r => r.slice())
    sortRows(rows, 1, 'desc')
    expect(rows).toEqual(before)
  })
})

describe('ResultTable sort interaction (#830)', () => {
  const columns = ['user', 'spend']
  const rows = [
    ['alice@test.com', '$581.70'],
    ['bob@test.com',   '$1,041.86'],
  ]

  function spendCells() {
    // data rows only (skip the header row)
    return screen.getAllByRole('row').slice(1).map(
      r => within(r).getAllByRole('cell')[1].textContent)
  }

  it('renders server order initially with aria-sort=none', () => {
    render(<ResultTable columns={columns} rows={rows} />)
    expect(spendCells()).toEqual(['$581.70', '$1,041.86'])
    for (const th of screen.getAllByRole('columnheader')) {
      expect(th).toHaveAttribute('aria-sort', 'none')
    }
  })

  it('click sorts ascending, second click descending, third resets', () => {
    render(<ResultTable columns={columns} rows={rows} />)
    const spendHeaderBtn = screen.getByRole('button', { name: /spend/i })

    fireEvent.click(spendHeaderBtn)            // asc
    expect(spendCells()).toEqual(['$581.70', '$1,041.86'])
    expect(screen.getAllByRole('columnheader')[1])
      .toHaveAttribute('aria-sort', 'ascending')

    fireEvent.click(spendHeaderBtn)            // desc — currency numeric
    expect(spendCells()).toEqual(['$1,041.86', '$581.70'])
    expect(screen.getAllByRole('columnheader')[1])
      .toHaveAttribute('aria-sort', 'descending')

    fireEvent.click(spendHeaderBtn)            // reset to server order
    expect(spendCells()).toEqual(['$581.70', '$1,041.86'])
    expect(screen.getAllByRole('columnheader')[1])
      .toHaveAttribute('aria-sort', 'none')
  })

  it('header sort control is a keyboard-operable button', () => {
    render(<ResultTable columns={columns} rows={rows} />)
    const btn = screen.getByRole('button', { name: /user/i })
    // a real <button> is focusable + Enter/Space-activatable for free
    expect(btn.tagName).toBe('BUTTON')
  })
})

// Part 2: the date-range control shared by all Cost Reports. The
// load-bearing states are default (month-to-date), a valid picked
// range, and an invalid range (the API also guards, but the UI must
// not fire the request — see CostReports.run).
describe('rangeState (#1081 date range)', () => {
  it('both blank = default month-to-date', () => {
    expect(rangeState('', '')).toEqual(
      { active: false, valid: true, mtd: true })
  })
  it('a valid range is active + valid', () => {
    expect(rangeState('2026-05-01', '2026-06-01')).toEqual(
      { active: true, valid: true, mtd: false })
  })
  it('start == end is valid', () => {
    expect(rangeState('2026-06-01', '2026-06-01').valid).toBe(true)
  })
  it('end before start is invalid', () => {
    expect(rangeState('2026-06-02', '2026-06-01').valid).toBe(false)
  })
  it('half-given range (one end blank) is invalid', () => {
    expect(rangeState('2026-06-01', '').valid).toBe(false)
    expect(rangeState('', '2026-06-01').valid).toBe(false)
  })
  it('non-ISO input is invalid', () => {
    expect(rangeState('yesterday', '2026-06-01').valid).toBe(false)
  })
})

describe('rangeLabel (date range)', () => {
  it('shows the span when a valid range is picked', () => {
    expect(rangeLabel('2026-05-01', '2026-06-01'))
      .toBe('2026-05-01 – 2026-06-01')
  })
  it('flags an invalid range', () => {
    expect(rangeLabel('2026-06-02', '2026-06-01')).toBe('Invalid range')
  })
})

import { readFileSync } from 'node:fs'

// The window is now ALWAYS explicit From/To dates (prefilled
// month-start..today on load) — no hidden "blank = month to date" and
// no "Month to date" button/label. Only the invalid-range warning is
// shown (a valid range is already visible in the date inputs).
describe('Cost Reports explicit-date window', () => {
  const src = readFileSync('src/pages/CostReports.jsx', 'utf8')

  it('dropped the "Month to date" button + label', () => {
    expect(src).not.toContain('Month to date')
    // resetRange keeps a "Reset to this month" affordance instead.
    expect(src).toContain('Reset to this month')
  })

  it('the status span is gated on the invalid range only', () => {
    expect(src).toContain('{!range.valid && (')
    // no leftover blank-means-MTD gate
    expect(src).not.toContain('{!range.mtd && (')
  })

  it('inputs prefill month-start .. today on load', () => {
    expect(src).toContain('useState(monthStartISO())')
    expect(src).toContain('useState(todayISO())')
  })
})

// Run lives in the From/To controls row, not the report header's
// top-right corner (the full page needs API context to render, so this
// asserts structure directly in source). Reset re-sets to this month
// AND runs in one click.
describe('Cost Reports Run placement + reset', () => {
  const src = readFileSync('src/pages/CostReports.jsx', 'utf8')

  it('resetRange re-sets to this month AND triggers a run (one click)', () => {
    const fn = src.slice(src.indexOf('function resetRange()'),
                         src.indexOf('function select('))
    expect(fn).toContain('monthStartISO()')
    expect(fn).toContain('todayISO()')
    expect(fn).toMatch(/run\(\s*\{\s*start:\s*s\s*,\s*end:\s*e\s*\}\s*\)/)
  })

  it('run accepts explicit start/end overrides', () => {
    expect(src).toMatch(/async function run\(\{[^}]*start[^}]*end[^}]*\}/)
  })

  it('Run button sits in the date-controls row, not the header', () => {
    const rowStart = src.indexOf('Part 2: date-range picker')
    expect(rowStart).toBeGreaterThan(-1)
    const header = src.slice(0, rowStart)
    const row = src.slice(rowStart)
    // header no longer renders Run / the force-refresh
    expect(header).not.toContain("{running ? 'Running…' : 'Run'}")
    expect(header).not.toContain('<RefreshCw')
    // the controls row carries Run, the reset affordance, force-refresh
    expect(row).toContain("{running ? 'Running…' : 'Run'}")
    expect(row).toContain('Reset to this month')
    expect(row).toContain('<RefreshCw')
  })
})

// Numeric cell formatting: token cols = integer + thousands separators
// (0 dp, never exponent); money cols = USD 2 dp with <$0.01 floor for
// sub-cent non-zero; never scientific notation (Athena returns tiny
// doubles as "4.0E-4").
describe('formatCell (numeric units & decimals)', () => {
  it('token columns: integer counts, thousands-separated, no exponent', () => {
    expect(formatCell('1234567', 'input_tokens')).toBe('1,234,567')
    expect(formatCell('0', 'output_tokens')).toBe('0')
    // a float artifact from an unrounded sum still renders as an integer
    expect(formatCell('0.08399999999999999', 'cache_read_tokens')).toBe('0')
    expect(formatCell('1024', 'tokens')).toBe('1,024')
  })

  it('money columns: 2 dp, <$0.01 for sub-cent non-zero, no exponent', () => {
    expect(formatCell('0.084', 'input_spend')).toBe('$0.08')
    expect(formatCell('1234.5', 'total_spend')).toBe('$1,234.50')
    expect(formatCell('0', 'total_spend')).toBe('$0.00')
    // sub-cent non-zero must NOT read as a bare $0.00 (hides real spend)
    expect(formatCell('0.0004', 'total_spend')).toBe('<$0.01')
    // the E-notation case (issue 2) never reaches the UI
    expect(formatCell('4.0E-4', 'actual_usd')).toBe('<$0.01')
  })

  it('non-numeric values pass through verbatim', () => {
    expect(formatCell('us.anthropic.claude', 'model')).toBe('us.anthropic.claude')
    expect(formatCell('', 'model')).toBe('')
  })

  it('plain number columns (line_items) integer-format, no exponent', () => {
    expect(formatCell('4096', 'line_items')).toBe('4,096')
  })
})

describe('headerLabel (units)', () => {
  it('adds ($) to a money column that lacks a $', () => {
    expect(headerLabel('total_spend')).toBe('total_spend ($)')
  })
  it('leaves token + non-money headers unchanged', () => {
    expect(headerLabel('input_tokens')).toBe('input_tokens')
    expect(headerLabel('model')).toBe('model')
  })
})

describe('date defaults (month-start .. today)', () => {
  const now = new Date('2026-06-22T12:00:00Z')
  it('monthStartISO = first of the current month', () => {
    expect(monthStartISO(now)).toBe('2026-06-01')
  })
  it('todayISO = the given day, ISO', () => {
    expect(todayISO(now)).toBe('2026-06-22')
  })
})
