import React, { createContext, useContext, useState, useMemo } from 'react'

const TeamScopeContext = createContext({
  selectedTeam: '*',
  setSelectedTeam: () => {},
  available: [],
  persona: 'team_admin',
})

export function TeamScopeProvider({ me, children }) {
  // Persona-aware default:
  //   org_admin → '*' (Org-all)
  //   team_admin with multiple teams → '*' (all visible by default)
  //   team_admin with one team → that team (the only one available)
  const initial = useMemo(() => {
    if (!me) return '*'
    if (me.persona === 'team_admin' && me.available_teams?.length === 1) {
      return me.available_teams[0].team_id
    }
    return '*'
  }, [me])

  const [selectedTeam, setSelectedTeam] = useState(initial)

  // Reset when me hydrates
  React.useEffect(() => {
    setSelectedTeam(initial)
  }, [initial])

  const value = useMemo(() => ({
    selectedTeam,
    setSelectedTeam,
    available: me?.available_teams || [],
    persona: me?.persona || 'team_admin',
    // #650: expose the caller so pages can compute the 3-tier
    // action gate (self / team-admin-of-this-user / org-admin).
    me: me || null,
  }), [selectedTeam, me])

  return (
    <TeamScopeContext.Provider value={value}>
      {children}
    </TeamScopeContext.Provider>
  )
}

export function useTeamScope() {
  return useContext(TeamScopeContext)
}
