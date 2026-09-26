export default function AccountChip({ user, onSignOut }) {
  return (
    <div className="account-chip">
      <div className="account-identity">
        <span className="account-tenant">{user.tenant_name}</span>
        <span className="account-email">{user.email}</span>
      </div>
      <span className="account-role">{user.role}</span>
      <button className="account-signout" type="button" onClick={onSignOut}>
        Sign out
      </button>
    </div>
  )
}
