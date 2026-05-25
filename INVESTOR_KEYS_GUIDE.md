# Investor Keys Guide

## Overview

Investor Keys allow you to grant temporary, configurable access to your dashboards without requiring investors to create accounts or purchase subscriptions. Perfect for demos, trials, and investor relations.

## For Investors: How to Use Your Key

### Accessing the Dashboard with Your Key

You've been provided with an investor key. Here's how to use it:

**Option 1: URL Query Parameter**
```
https://polymarket.example.com/?superuser_key=YOUR_KEY_HERE
```

**Option 2: HTTP Header (for API access)**
```bash
curl -H "Authorization: Bearer YOUR_KEY_HERE" https://polymarket.example.com/
```

**Option 3: Command Line Tools**
```bash
# Using wget
wget --header="Authorization: Bearer YOUR_KEY_HERE" https://polymarket.example.com/

# Using fetch in Node.js
fetch('https://polymarket.example.com/', {
  headers: { 'Authorization': 'Bearer YOUR_KEY_HERE' }
})
```

### What You Can Access

Your key grants access based on its configuration:

- **Dashboards**: You can view one or more specific dashboards, or all dashboards
- **Aspects**: Your access may have restrictions like:
  - `read-only` - View data but can't make changes
  - `demo-mode` - Shows sample/demo data only
  - `no-trading` - Can't execute trades
  - `limited-data` - Restricted data access
  - `trial` - Trial period with full or limited features

### Expiration

Investor keys have expiration dates. If you see an access denied message, your key may have expired. Contact your administrator to request a renewal or new key.

### Security

- Keep your investor key confidential
- Don't share it publicly or include it in version control
- If compromised, notify your administrator immediately
- Never commit keys to git repositories

## For Administrators: Creating and Managing Keys

### Admin Dashboard

1. Log in to the admin panel at `/admin`
2. Click the **"Investor Keys"** tab
3. Fill in the form to create a new key

### Creating a Key

**Form Fields:**

| Field | Required | Example | Notes |
|-------|----------|---------|-------|
| **Custom Key** | No | `julian-habbig` | Human-readable key. Leave blank for random token. |
| **Key Name** | No | `Q2 2024 Investors` | Display name for your reference |
| **Dashboards** | No | `polymarket,crypto` | Comma-separated. Leave blank for all. |
| **Aspects** | No | `read-only,demo-mode` | Comma-separated. See aspects list below. |
| **Expiration** | No | `90 days` | When the key stops working. |

### Key Management

**Enable/Disable:**
- Click **"Disable"** to temporarily revoke access without deleting the key
- Click **"Enable"** to restore access
- Changes take effect immediately

**Delete:**
- Click **"Delete"** to permanently remove the key
- Cannot be undone - investors will lose access immediately

**Filter:**
- Use status filters to find keys: All, Active, Disabled, Expired
- Track last usage to identify active vs. inactive keys

### Example Scenarios

**Scenario 1: Demo for potential client (Q2 2024)**
```
Custom Key: demo-client-q2
Key Name: Demo Client - Q2 2024
Dashboards: (leave blank for all)
Aspects: demo-mode,read-only
Expiration: 7 days
```

**Scenario 2: Investor relations - limited access**
```
Custom Key: investor-relations-2024
Key Name: Investor Relations Team
Dashboards: polymarket,crypto,midterm
Aspects: limited-data
Expiration: 90 days
```

**Scenario 3: API integration for partner**
```
Custom Key: partner-api-integration
Key Name: Partner API Access
Dashboards: polymarket
Aspects: (none)
Expiration: 180 days (6 months)
```

## Available Aspects

Aspects are permission/feature flags that customize investor access:

| Aspect | Description | Use Case |
|--------|-------------|----------|
| `read-only` | View-only access, no writes | Prevent accidental/intentional changes |
| `demo-mode` | Shows sample/demo data | Product demonstrations |
| `no-trading` | Can't place trades or orders | Limited functionality trials |
| `limited-data` | Restricted data visibility | Privacy-conscious investors |
| `trial` | Trial period flag | Temporary trial access |
| `vip` | Premium investor features | Special investors |
| `view-only-summary` | Only summary stats visible | Executive overviews |
| `no-export` | Can't download/export data | IP protection |
| `audit-enabled` | All actions logged | Compliance requirements |

### Custom Aspects

You can define your own aspects! The system is flexible:
- Use lowercase alphanumeric characters and hyphens
- Examples: `client-x`, `research-team`, `partner-demo`
- Dashboards will check for these and customize behavior

## API Usage

### Generate a Key (Admin API)

```bash
curl -X POST https://example.com/admin/superuser-keys/generate \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "name=My Key&custom_key=my-key&dashboards=polymarket&aspects=read-only&expires_in_days=90"
```

### List Keys

```bash
curl https://example.com/admin/api/superuser-keys
```

### Enable/Disable Key

```bash
curl -X POST https://example.com/admin/superuser-keys/1/toggle
curl -X POST https://example.com/admin/superuser-keys/1/enable
curl -X POST https://example.com/admin/superuser-keys/1/disable
```

## Security Best Practices

### For Investors

✅ **DO:**
- Keep your key in a secure location
- Use environment variables to store keys
- Rotate keys periodically
- Use short expiration dates for sensitive access

❌ **DON'T:**
- Share keys in plain text
- Commit keys to git/version control
- Use the same key across multiple environments
- Reuse keys after rotation
- Post keys in public forums or issues

### For Administrators

✅ **DO:**
- Use descriptive key names for audit trails
- Set reasonable expiration dates
- Review last-used timestamps
- Disable unused keys regularly
- Create separate keys for different purposes

❌ **DON'T:**
- Create keys with indefinite expiration
- Share admin panel with untrusted users
- Reuse the same key for multiple investors
- Ignore expired keys (clean them up)
- Allow investors direct admin access

## Troubleshooting

### "Access Denied" with Valid Key

Possible causes:
1. **Key Expired** - Check the expiration date in admin panel
2. **Key Disabled** - Admin may have disabled it; request re-enabling
3. **Dashboard Restrictions** - Your key may not include this specific dashboard
4. **Session Cache** - Clear browser cache and cookies

### Key Not Working in Header

Make sure you're using the correct format:
```
Authorization: Bearer YOUR_KEY_HERE
```
(NOT `Authorization: YOUR_KEY_HERE`)

### Want to Track Key Usage

The admin panel shows:
- `Last used:` timestamp for each key
- `Created:` when the key was generated
- `Expires:` expiration date (if set)

Use this to identify which investors are actively using their keys.

## FAQ

**Q: Can I change a key's expiration date after creation?**  
A: Not yet - delete and recreate with new expiration, or contact support.

**Q: What happens when a key expires?**  
A: Investors will see an "Access Denied" message and won't be able to view the dashboard.

**Q: Can I track what investors do with their keys?**  
A: Yes - the `X-Gateway-Investor-Mode` header is sent to dashboards, allowing audit logging.

**Q: How many keys can I create?**  
A: Unlimited - create as many as needed.

**Q: Can I give a key to multiple investors?**  
A: Technically yes, but not recommended - create separate keys for each investor for audit trails.

**Q: What if an investor loses their key?**  
A: Generate a new key and send it to them. The old key can be deleted.

---

**Questions?** Contact your Habbig administrator.
