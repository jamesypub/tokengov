import React, { useEffect, useState } from 'react'
import { api } from './api'
import { Layout, roleOf, firstNavPath } from './Layout'
import { TeamScopeProvider } from './TeamScope'
import Users from './pages/Users'
import UserDetail from './pages/UserDetail'
import Teams from './pages/Teams'
import Activity from './pages/Activity'
import OrgSettings from './pages/OrgSettings'
import CostReports from './pages/CostReports'
import Jobs from './pages/Jobs'
import VelocityCost from './pages/VelocityCost'
import Integrations from './pages/Integrations'
import Diagnostics from './pages/Diagnostics'
import MemberHome from './pages/MemberHome'
import DesktopAuthScreen, {
  fetchDesktopAuthStatus,
} from './pages/DesktopAuthScreen'
import Login from './pages/Login'

const IS_DESKTOP =
  typeof window !== 'undefined' &&
  window.__TG_DEPLOYMENT__ === 'desktop'

const ROUTES = {
  '/':             { component: Activity },
  '/activity':     { component: Activity },
  '/users':        { component: Users },
  '/teams':        { component: Teams },
  '/settings':     { component: OrgSettings },
  '/cost-reports': { component: CostReports },
  '/jobs':         { component: Jobs },
  '/velocity-cost':       { component: VelocityCost },
  '/velocity-cost/cost':  { component: VelocityCost },
  '/velocity-cost/speed': { component: VelocityCost },
  '/velocity-cost/jira':  { component: VelocityCost },
  '/integrations':        { component: Integrations },
  '/diagnostics':         { component: Diagnostics },
}

function getPath() {
  const h = window.location.hash || '#/'
  const stripped = h.replace(/^#/, '') || '/'
  // Strip a query string for route lookup; the page itself
  // can still read window.location.hash if it needs the params.
  const q = stripped.indexOf('?')
  return q === -1 ? stripped : stripped.slice(0, q)
}

function matchUserDetail(path) {
  return /^\/users\/(.+)$/.exec(path)
}

export default function App() {
  const [me, setMe] = useState(null)
  const [meErr, setMeErr] = useState(null)
  const [loading, setLoading] = useState(true)
  const [path, setPath] = useState(getPath())
  // Desktop-only: null = not yet checked, then {ok, reason, ...}.
  const [authStatus, setAuthStatus] = useState(
    IS_DESKTOP ? null : { ok: true })

  async function checkDesktopAuth() {
    const s = await fetchDesktopAuthStatus()
    setAuthStatus(s)
    return s
  }

  useEffect(() => {
    if (!IS_DESKTOP) return
    checkDesktopAuth()
  }, [])

  useEffect(() => {
    if (!authStatus?.ok) return
    api.whoami()
      .then(setMe)
      .catch(e => setMeErr(String(e)))
      .finally(() => setLoading(false))
  }, [authStatus?.ok])

  useEffect(() => {
    function onHash() { setPath(getPath()) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // #651: land on the first nav item visible to the role (Users
  // for admins) instead of Activity. Only redirect the bare root
  // landing once whoami resolves — deep links (#/teams, etc.) and
  // an explicit #/activity are untouched. replace() so Back doesn't
  // trap the user on '/'.
  // #1056: feature flags from whoami; drives flag-gated nav + route
  // guard. Default OFF until whoami resolves.
  const flags = { vc_enabled: !!me?.vc_enabled }

  useEffect(() => {
    if (loading || path !== '/') return
    // #929: members render the dedicated MemberHome (no admin routes),
    // so don't redirect them into the admin nav.
    if (me && me.persona === 'member') return
    const dest = firstNavPath(roleOf(me, loading), flags)
    if (dest !== '/') {
      window.location.replace('#' + dest)
    }
  }, [loading, path, me])

  // #1056: route guard — when vc_enabled is OFF, /velocity-cost* is
  // not reachable by URL; redirect to the role's default landing (no
  // 403, no blank). Wait for whoami so we don't bounce before the
  // flag is known.
  useEffect(() => {
    if (loading) return
    if (me && me.persona === 'member') return
    if (path.startsWith('/velocity-cost') && !flags.vc_enabled) {
      const dest = firstNavPath(roleOf(me, loading), flags)
      window.location.replace('#' + (dest === '/velocity-cost' ? '/' : dest))
    }
  }, [loading, path, me])

  if (IS_DESKTOP && authStatus && !authStatus.ok) {
    return (
      <DesktopAuthScreen
        status={authStatus}
        onRetry={checkDesktopAuth}
      />
    )
  }

  // Cloud-only login page. URL pathname (not hash) is /login;
  // the FastAPI SPA fallback serves index.html for any path,
  // so we detect via window.location.pathname.
  if (!IS_DESKTOP && typeof window !== 'undefined' &&
      window.location.pathname === '/login') {
    return <Login />
  }

  // #929: a member sees the dedicated member view — own data + the two
  // self-edit fields — NOT the admin SPA shell (no admin nav, no
  // admin routes). Server-side enforcement (the #927 Scope) is the
  // real gate; this just renders the right surface for the persona.
  // Wait for whoami (loading) so we don't flash the admin shell first.
  if (!loading && me && me.persona === 'member') {
    return <MemberHome me={me} />
  }

  const m = matchUserDetail(path)
  if (m) {
    return (
      <TeamScopeProvider me={me}>
        <Layout me={me} meErr={meErr} loading={loading} path="/users">
          <UserDetail email={decodeURIComponent(m[1])} />
        </Layout>
      </TeamScopeProvider>
    )
  }

  const route = ROUTES[path] || ROUTES['/']
  const PageComponent = route.component

  return (
    <TeamScopeProvider me={me}>
      <Layout me={me} meErr={meErr} loading={loading} path={path}>
        <PageComponent />
      </Layout>
    </TeamScopeProvider>
  )
}
