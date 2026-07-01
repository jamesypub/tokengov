import React, { useEffect, useState } from 'react'
import {
  BarChart3, Users, ShieldCheck,
  Settings, Menu, X, LogOut, Zap, DollarSign,
  Gauge, Plug, Stethoscope,
} from 'lucide-react'
import { getVersion, displayVersion, api } from './api'
import { useTeamScope } from './TeamScope'
import { useIsMobile } from './ui/useIsMobile'
import { cn } from './ui/cn'
import { getImpersonation, setImpersonation } from './dev'

const APP_NAME = 'Token Governance'

const IS_DESKTOP =
  typeof window !== 'undefined' &&
  window.__TG_DEPLOYMENT__ === 'desktop'

async function handleSignOut() {
  if (IS_DESKTOP) {
    alert(
      'On the desktop binary, sign out via:\n\n' +
      'aws sso logout --sso-session tg-sso\n\n' +
      'Then close this window.'
    )
    return
  }
  // Logout always clears the local tg session server-side. When a SAML
  // IdP with single-logout is active, the response carries a Cognito
  // hosted logout URL — navigate there so the IdP (IDC) session is
  // terminated too, then it returns the browser to /login. Otherwise
  // (204/no body) go straight to /login.
  let body = null
  try { body = await api.logout() } catch {}
  window.location.href = (body && body.logout_url) || '/login'
}

// #651: Users is the first menu item (was 3rd). Admins land on it
// at login — see firstNavPath / the App default-route redirect.
const SECTIONS = [
  {
    label: 'Observe',
    items: [
      { path: '/users',        label: 'Users',        Icon: Users,       roles: ['org_admin','team_admin'] },
      // #1056: gated on the vc_enabled experimental flag (default OFF).
      { path: '/velocity-cost', label: 'Velocity & Cost', Icon: Gauge,   flag: 'vc_enabled' },
      { path: '/activity',     label: 'Activity',     Icon: BarChart3 },
      { path: '/teams',        label: 'Teams',        Icon: ShieldCheck, roles: ['org_admin','team_admin'] },
      { path: '/cost-reports', label: 'Cost Reports', Icon: DollarSign,  roles: ['org_admin'] },
    ],
  },
  {
    label: 'Govern',
    items: [
      { path: '/settings',     label: 'Settings',     Icon: Settings, roles: ['org_admin'] },
      { path: '/integrations', label: 'Integrations', Icon: Plug,     roles: ['org_admin'] },
      { path: '/jobs',         label: 'Jobs',         Icon: Zap,      roles: ['org_admin'] },
      { path: '/diagnostics',  label: 'Diagnostics',  Icon: Stethoscope, roles: ['org_admin'] },
    ],
  },
]

export function roleOf(me, isLoading) {
  if (isLoading) return null
  if (!me) return 'member'
  if (me.org_admin || me.persona === 'org_admin') return 'org_admin'
  if (me.persona === 'team_admin') return 'team_admin'
  return 'member'
}

// #1056: `flags` is the runtime feature-flag map (from whoami, e.g.
// { vc_enabled }). An item with a `flag` is hidden unless that flag
// is truthy — a SECOND visibility axis alongside `roles`. Missing
// flags map → flag-gated items are hidden (default OFF, matches the
// "render after the flag resolves" / hidden-until-known UX).
export function itemVisible(item, role, flags = {}) {
  if (item.flags) { /* reserved for future multi-flag items */ }
  if (item.flag && !flags[item.flag]) return false
  if (!item.roles) return true
  return item.roles.includes(role)
}

// #651/#1056: the path of the first nav item visible to this role
// AND enabled by the current flags, in menu order. Admins
// (org_admin/team_admin) see Users first, so they land there at
// login. A plain member can't see Users and — now that Velocity &
// Cost is flag-gated (#1056) — falls through to Activity (no role
// gate, no flag), NOT a hidden V&C page. Returns '/' if nothing is
// visible (shouldn't happen — Activity is always visible).
export function firstNavPath(role, flags = {}) {
  for (const section of SECTIONS) {
    for (const item of section.items) {
      if (itemVisible(item, role, flags)) return item.path
    }
  }
  return '/'
}

function NavItem({ item, active, onClick, badge, badgeLabel }) {
  return (
    <a
      href={`#${item.path}`}
      onClick={onClick}
      className={cn('tg-nav__item', active && 'is-active')}
    >
      <item.Icon size={15} />
      <span>{item.label}</span>
      {badge > 0 && (
        <span
          className="tg-nav__badge"
          aria-label={badgeLabel ? badgeLabel(badge) : String(badge)}
          // #818: a visible (non-aria-only) tooltip so the badge's
          // meaning is legible on hover — clicking the nav item lands
          // on /users, where the drift banner explains it in full.
          title={badgeLabel ? badgeLabel(badge) : String(badge)}
        >
          {badge}
        </span>
      )}
    </a>
  )
}

function SidebarContents({ me, path, onNavClick, version, isLoading }) {
  const role = roleOf(me, isLoading)
  // #1056: feature flags from whoami drive flag-gated nav items.
  const flags = { vc_enabled: !!me?.vc_enabled }
  const { selectedTeam, setSelectedTeam, available, persona } = useTeamScope()
  const devEnabled = me?.auth_method === 'test'
  const [personas, setPersonas] = useState([])
  const impersonation = getImpersonation()

  // #726 (#720 slice 4): the #624 pricing-pending badge is retired
  // with the auto-pricing pipeline — CUR carries billed spend
  // directly, so there's no "models awaiting pricing" to nudge.

  // #649: count of principals drifted in the latest governance
  // sweep (intent vs IAM truth). Drives a badge on the Users nav
  // item so an org-admin sees "N no longer enforced" at login.
  // Org-admin scoped (endpoint 403s for others).
  const [driftCount, setDriftCount] = useState(0)
  useEffect(() => {
    if (role !== 'org_admin') { setDriftCount(0); return }
    api.governanceDriftCount()
      .then(d => setDriftCount(d?.count || 0))
      .catch(() => setDriftCount(0))
  }, [role])

  useEffect(() => {
    if (!devEnabled) return
    api.listPersonas()
      .then(d => setPersonas(d?.personas || []))
      .catch(() => setPersonas([]))
  }, [devEnabled])

  return (
    <>
      <div className="tg-brand">
        <div className="tg-brand__name">
          <span className="dot"></span>{APP_NAME}
        </div>
        <div className="tg-brand__tagline">Velocity Measured</div>
      </div>

      <nav className="tg-nav">
        {isLoading ? (
          <div style={{ padding: '14px 0' }}>
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="tg-nav__skeleton" />
            ))}
          </div>
        ) : (
          SECTIONS.map(section => {
            const visible = section.items.filter(i => itemVisible(i, role, flags))
            if (visible.length === 0) return null
            return (
              <React.Fragment key={section.label}>
                <div className="tg-nav__section-label">{section.label}</div>
                {visible.map(item => (
                  <NavItem
                    key={item.path}
                    item={item}
                    active={
                      path === item.path ||
                      path.startsWith(item.path + '/')
                    }
                    onClick={onNavClick}
                    badge={item.path === '/users' ? driftCount : 0}
                    badgeLabel={
                      item.path === '/users'
                        ? (n) => `${n} principal${n === 1 ? '' : 's'} with governance drift`
                      : undefined
                    }
                  />
                ))}
              </React.Fragment>
            )
          })
        )}
      </nav>

      <div className="tg-foot">
        {available.length > 0 && role !== 'member' && (
          <div>
            <div className="tg-foot__label">Team</div>
            {persona === 'team_admin' && available.length === 1 ? (
              <div style={{
                fontSize: 12, color: 'var(--ink-2)',
                padding: '6px 0',
              }}>
                {available[0].name || available[0].team_id}
              </div>
            ) : (
              <select
                value={selectedTeam}
                onChange={e => setSelectedTeam(e.target.value)}
                className="tg-foot__select"
              >
                <option value="*">
                  {role === 'org_admin' ? 'Org (all teams)' : 'All my teams'}
                </option>
                {available.map(t => (
                  <option key={t.team_id} value={t.team_id}>{t.name || t.team_id}</option>
                ))}
              </select>
            )}
          </div>
        )}

        {devEnabled && personas.length > 0 && (
          <div>
            <div className="tg-foot__label tg-foot__select-dev-label">View as (dev)</div>
            <select
              value={impersonation}
              onChange={e => setImpersonation(e.target.value)}
              className="tg-foot__select-dev"
              title="Impersonates the chosen admin via X-Tg-Test-Email. Reloads on change."
            >
              <option value="">— bootstrap admin —</option>
              {personas.map(p => (
                <option key={p.email + (p.team_id || '')} value={p.email}>
                  {p.role === 'org_admin' ? '[org] ' : `[${p.team_id}] `}
                  {p.email}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="tg-foot__user">
          <a
            href="#/profile"
            className="tg-foot__avatar"
            title="Profile"
          >
            {(me?.email || '?')[0].toUpperCase()}
          </a>
          <div className="tg-foot__user-info">
            <div className="tg-foot__email" title={me?.email}>
              {me?.email || 'loading…'}
            </div>
            <div className="tg-foot__role">
              {(role || '').replace(/_/g, ' ')}
            </div>
          </div>
          <button
            onClick={handleSignOut}
            title="Sign out"
            className="tg-foot__signout"
          >
            <LogOut size={14} />
          </button>
        </div>

        {version && (
          <div className="tg-foot__version">{displayVersion(version)}</div>
        )}
      </div>
    </>
  )
}

export function Layout({ me, meErr, loading, path, children }) {
  const [version, setVersion] = useState(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const isMobile = useIsMobile()

  useEffect(() => { getVersion().then(setVersion) }, [])

  if (isMobile) {
    return (
      <div className="tg-shell--mobile">
        <div className="tg-mobile-bar">
          <button onClick={() => setDrawerOpen(true)}>
            <Menu size={20} />
          </button>
          <span className="tg-mobile-bar__brand">{APP_NAME}</span>
          <a href="#/profile" className="tg-foot__avatar" title="Profile">
            {(me?.email || '?')[0].toUpperCase()}
          </a>
        </div>

        {drawerOpen && (
          <div
            className="tg-drawer-overlay"
            onClick={() => setDrawerOpen(false)}
          >
            <div
              className="tg-drawer"
              onClick={e => e.stopPropagation()}
            >
              <div className="tg-drawer__close">
                <button onClick={() => setDrawerOpen(false)}>
                  <X size={18} />
                </button>
              </div>
              <SidebarContents
                me={me}
                path={path}
                onNavClick={() => setDrawerOpen(false)}
                version={version}
                isLoading={loading}
              />
            </div>
            <div className="tg-drawer__scrim" />
          </div>
        )}

        <main>{children}</main>
      </div>
    )
  }

  return (
    <div className="tg-shell">
      <aside className="tg-shell__aside">
        <SidebarContents
          me={me}
          path={path}
          onNavClick={null}
          version={version}
          isLoading={loading}
        />
      </aside>
      <div className="tg-shell__spacer" />
      <main className="tg-shell__main">
        {children}
      </main>
    </div>
  )
}
