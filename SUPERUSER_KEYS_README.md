# Superuser Keys System

Complete investor access management system for Habbig dashboards.

## Quick Links

- **For Investors**: [INVESTOR_KEYS_GUIDE.md](./INVESTOR_KEYS_GUIDE.md) - How to use your key
- **For Admins**: [Admin UI Guide](#admin-ui) below  
- **For Developers**: [DASHBOARD_ASPECT_INTEGRATION.md](./DASHBOARD_ASPECT_INTEGRATION.md) - Integrate with dashboards
- **For Testing**: [SUPERUSER_KEY_TESTING_GUIDE.md](./SUPERUSER_KEY_TESTING_GUIDE.md) - Test the system

## What Are Superuser Keys?

Superuser keys are API tokens that grant temporary, configurable access to dashboards without requiring:
- User account creation
- Email verification
- Subscription payment
- Long-term credentials

Perfect for:
- **Product Demos** - Show prospects your platform
- **Investor Relations** - Grant investor access for evaluation
- **Customer Trials** - Let customers try before buying
- **Partner Integrations** - API access for partners
- **Research Projects** - Temporary access for researchers

## Key Features

✅ **Custom Naming** - Human-readable keys like `julian-habbig`  
✅ **Fine-Grained Control** - Limit to specific dashboards  
✅ **Permission Aspects** - read-only, demo-mode, no-trading, etc.  
✅ **Time-Based Expiration** - Set when access ends  
✅ **Enable/Disable Toggle** - Revoke without deleting  
✅ **Audit Trail** - Track last usage of each key  
✅ **Admin Dashboard** - Create and manage keys easily  

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Investor / Partner                     │
│        Uses key: ?superuser_key=julian-habbig          │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
        ┌────────────────────────────────┐
        │  Gateway (server.py)           │
        │                                │
        │  1. Extract key from URL/header│
        │  2. Validate against DB        │
        │  3. Check expiration          │
        │  4. Check dashboards          │
        │  5. Forward with X-Gateway-*  │
        │     headers                    │
        └────────────────────────────────┘
                     │
      ┌──────────────┴──────────────┐
      │                             │
      ▼                             ▼
┌──────────────────┐      ┌──────────────────┐
│  Database        │      │  Dashboards      │
│  superuser_keys  │      │  (polymarket,   │
│  table           │      │   crypto, etc.)  │
│                  │      │                  │
│  - id            │      │ Check:           │
│  - key           │      │ - X-Gateway-*    │
│  - name          │      │ - Aspects        │
│  - dashboards    │      │ - Apply rules    │
│  - aspects       │      │ - Hide features  │
│  - expires_at    │      │ - Log actions    │
│  - active        │      └──────────────────┘
│  - last_used_at  │
└──────────────────┘
```

## Database Schema

```sql
CREATE TABLE superuser_keys (
    id              INTEGER PRIMARY KEY,
    key             TEXT UNIQUE,           -- e.g., 'julian-habbig'
    name            TEXT,                  -- e.g., 'Q2 2024 Investors'
    dashboards      TEXT,                  -- 'polymarket,crypto' (empty = all)
    aspects         TEXT,                  -- 'read-only,demo-mode'
    created_at      INTEGER,               -- Unix timestamp
    expires_at      INTEGER,               -- NULL = never expires
    last_used_at    INTEGER,               -- When key was last validated
    active          INTEGER                -- 1=active, 0=disabled
);
```

## Admin UI

### Creating a Key

1. Log in to admin panel: `/admin`
2. Click **"Investor Keys"** tab
3. Fill in the form:

| Field | Required | Example |
|-------|----------|---------|
| Custom Key | No | `julian-habbig` |
| Key Name | No | `Q2 2024 Investors` |
| Dashboards | No | `polymarket,crypto` |
| Aspects | No | `read-only,demo-mode` |
| Expiration | No | `90 days` |

4. Click **"Create Key"**
5. Copy the key from the success banner

### Managing Keys

**Filter**: View keys by status (All, Active, Disabled, Expired)

**Enable/Disable**: Temporarily revoke access without deleting

**Delete**: Permanently remove (can't be undone)

**Track Usage**: See when each key was last used

## API Access

Investors can use keys in multiple ways:

### URL Query Parameter
```
https://polymarket.example.com/?superuser_key=julian-habbig
```

### HTTP Header (Bearer Token)
```bash
curl -H "Authorization: Bearer julian-habbig" \
  https://polymarket.example.com/
```

### Command Line
```bash
wget --header="Authorization: Bearer julian-habbig" \
  https://polymarket.example.com/
```

## Headers Sent to Dashboards

When a key is used, dashboards receive:

```
X-Gateway-Investor-Mode: true
X-Gateway-Key-Aspects: read-only,demo-mode
X-Gateway-User-Id: superuser
X-Gateway-User-Email: investor@dashboard
X-Gateway-Secret: (shared signing secret)
```

Dashboards use these to:
- Show investor mode UI
- Restrict features based on aspects
- Log investor actions
- Apply data filtering

## Aspects (Permission Flags)

| Aspect | Purpose | Use Case |
|--------|---------|----------|
| `read-only` | View-only, no writes | Prevent changes |
| `demo-mode` | Sample data only | Product demos |
| `no-trading` | Can't place trades | Limited trials |
| `limited-data` | Restricted data | Privacy-conscious |
| `trial` | Trial period | Time-limited access |
| `vip` | Premium features | Special investors |
| `view-only-summary` | Summary only | Executive view |
| `no-export` | No downloads | IP protection |
| `audit-enabled` | Log all actions | Compliance |
| Custom | Your own aspects | Anything you want |

## Code Examples

### Python - Check Aspects

```python
import sys
sys.path.insert(0, 'gateway')
import db

# Check if key has aspect
has_readonly = db.key_has_aspect('julian-habbig', 'read-only')

# Get all aspects
aspects = db.get_key_aspects('julian-habbig')

# Validate key
info = db.validate_superuser_key('julian-habbig')
if info:
    print(f"Valid until: {info['expires_at']}")
```

### JavaScript - Check in Dashboard

```javascript
// Fetch aspects from server
async function hasAspect(aspect) {
  const res = await fetch('/api/auth/aspects');
  const data = await res.json();
  return data.aspects.includes(aspect);
}

// Use in UI
if (await hasAspect('read-only')) {
  document.getElementById('trade-btn').disabled = true;
}
```

### FastAPI - Protect Routes

```python
from fastapi import Request, HTTPException

@app.post("/api/trade")
async def place_trade(request: Request):
    aspects_header = request.headers.get("X-Gateway-Key-Aspects", "")
    aspects = [a.strip() for a in aspects_header.split(",") if a.strip()]
    
    if "read-only" in aspects:
        raise HTTPException(403, "Read-only keys cannot trade")
    
    # Process trade...
```

## Getting Started

### 1. Install / Setup

No additional installation needed - system is built-in!

### 2. Create Your First Key

```bash
# Via admin UI: /admin → Investor Keys tab
# Or via curl (if you have admin access):

curl -X POST http://localhost:7000/admin/superuser-keys/generate \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "custom_key=test-investor&name=Test%20Investor&expires_in_days=7"
```

### 3. Test Access

```bash
# Via URL
http://localhost:7000/?superuser_key=test-investor

# Via header
curl -H "Authorization: Bearer test-investor" http://localhost:7000/
```

### 4. Integrate with Dashboards

See [DASHBOARD_ASPECT_INTEGRATION.md](./DASHBOARD_ASPECT_INTEGRATION.md) for full guide.

## Security Considerations

### For Investors

- ✅ Use short expiration dates
- ✅ Store keys in environment variables, not code
- ✅ Notify admin if key is compromised
- ❌ Don't commit keys to git
- ❌ Don't share in plain text

### For Administrators

- ✅ Create separate keys for each investor
- ✅ Review unused keys regularly
- ✅ Set reasonable expiration dates
- ✅ Monitor last-used timestamps
- ❌ Don't create indefinite keys
- ❌ Don't reuse keys across environments

### Implementation

The system uses:
- Secure key validation (constant-time comparison)
- Database-backed storage (easily revocable)
- Expiration checking (time-based access)
- Aspect checking (fine-grained permissions)
- Logging (audit trail)

## Testing

See [SUPERUSER_KEY_TESTING_GUIDE.md](./SUPERUSER_KEY_TESTING_GUIDE.md) for:
- Unit tests
- Integration tests
- Performance tests
- Test scenarios
- Debugging tips

Quick test:
```bash
# Create a test key
curl -X POST http://localhost:7000/admin/superuser-keys/generate \
  -d "custom_key=quick-test&name=Quick%20Test"

# Use it
curl -H "Authorization: Bearer quick-test" http://localhost:7000/
```

## Troubleshooting

### Key Not Working

1. Check if key is active: Admin UI → Investor Keys → check status dot
2. Check expiration: See "Expires:" date in key list
3. Clear browser cache and cookies
4. Check key format in URL/header - should be exact match

### "Access Denied" Error

Possible causes:
- Key is disabled or expired
- Key doesn't include this specific dashboard
- Dashboard blocking based on aspects

### Performance Issues

Keys are cached for 60 seconds to minimize DB queries. Check:
```python
import sys
sys.path.insert(0, 'gateway')
import db

# Validate should be fast (< 1ms due to caching)
import time
start = time.time()
db.validate_superuser_key('your-key')
print(f"Validation time: {(time.time() - start) * 1000:.2f}ms")
```

## Support

- **Admin Questions**: Check [INVESTOR_KEYS_GUIDE.md](./INVESTOR_KEYS_GUIDE.md) admin section
- **Developer Integration**: See [DASHBOARD_ASPECT_INTEGRATION.md](./DASHBOARD_ASPECT_INTEGRATION.md)
- **Testing Issues**: See [SUPERUSER_KEY_TESTING_GUIDE.md](./SUPERUSER_KEY_TESTING_GUIDE.md)
- **Investor Support**: Share [INVESTOR_KEYS_GUIDE.md](./INVESTOR_KEYS_GUIDE.md) with investors

## Files Modified

### Core Implementation
- `gateway/db.py` - Database functions for key management
- `gateway/server.py` - API endpoints and proxy integration

### Admin Interface
- `gateway/static/admin.html` - Admin dashboard UI

### Documentation (This Package)
- `INVESTOR_KEYS_GUIDE.md` - For investors
- `SUPERUSER_KEY_TESTING_GUIDE.md` - For QA/testing
- `DASHBOARD_ASPECT_INTEGRATION.md` - For dashboard developers
- `SUPERUSER_KEYS_README.md` - This file

## Examples

### Example 1: Product Demo

```
Key: product-demo-april
Name: April 2024 Product Demo
Dashboards: (all)
Aspects: demo-mode, read-only
Expires: April 30, 2024
```

### Example 2: Investor Evaluation

```
Key: investor-q2-2024
Name: Q2 2024 Investor Access
Dashboards: polymarket, crypto, midterm
Aspects: limited-data
Expires: June 30, 2024
```

### Example 3: Partner API Integration

```
Key: partner-api-prod
Name: Partner API Production Access
Dashboards: polymarket
Aspects: (none)
Expires: December 31, 2024
```

---

**Ready to go!** Start with the admin UI, then read the investor guide. 🚀
