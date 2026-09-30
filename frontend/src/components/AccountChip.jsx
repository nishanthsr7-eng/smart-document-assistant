import { t } from '../i18n'

export default function AccountChip({ user, onSignOut }) {
  return (
    <div className="account-chip" aria-label={t('account.label', { email: user.email, tenant: user.tenant_name })}>
      <div className="account-identity">
        <span className="account-tenant">{user.tenant_name}</span>
        <span className="account-email">{user.email}</span>
      </div>
      <span className="account-role">{user.role}</span>
      <button className="account-signout" type="button" onClick={onSignOut}>
        {t('account.signOut')}
      </button>
    </div>
  )
}
