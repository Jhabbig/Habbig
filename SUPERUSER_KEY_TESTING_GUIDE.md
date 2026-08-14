# Superuser Key Testing Guide

## Quick Start Testing

### 1. Generate a Test Key (Manual)

```bash
# Via curl to admin endpoint
curl -X POST http://localhost:7000/admin/superuser-keys/generate \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "_csrf_token=YOUR_CSRF_TOKEN&custom_key=test-key&name=Test%20Key&dashboards=polymarket&aspects=read-only&expires_in_days=7"
```

### 2. Test Access via URL Parameter

```bash
# Access dashboard with key
http://localhost:7000/?superuser_key=test-key

# Or with specific subdomain
http://polymarket.localhost:7000/?superuser_key=test-key
```

### 3. Test Access via HTTP Header

```bash
curl -H "Authorization: Bearer test-key" http://localhost:7000/
```

### 4. Verify Headers in Dashboards

```bash
curl -i -H "Authorization: Bearer test-key" http://localhost:7000/ | grep "X-Gateway"

# Should show:
# X-Gateway-Investor-Mode: true
# X-Gateway-Key-Aspects: read-only
# X-Gateway-User-Id: superuser
# X-Gateway-User-Email: investor@dashboard
```

## Test Scenarios

### Scenario 1: Basic Access (No Restrictions)

```bash
# 1. Create key with no dashboards/aspects
custom_key: demo-access
name: Demo Access
dashboards: (empty)
aspects: (empty)
expires_in_days: 7

# 2. Test access
curl -H "Authorization: Bearer demo-access" http://localhost:7000/

# Expected: 200 OK, full access to all dashboards
```

### Scenario 2: Limited Dashboard Access

```bash
# 1. Create key restricted to 2 dashboards
custom_key: limited-dashboards
name: Limited Access
dashboards: polymarket,crypto
aspects: (empty)

# 2. Test accessing polymarket (should work)
curl -H "Authorization: Bearer limited-dashboards" http://polymarket.localhost:7000/

# 3. Test accessing climate (should fail)
curl -H "Authorization: Bearer limited-dashboards" http://climate.localhost:7000/
# Expected: 302 redirect to billing

# 4. Test database
python3 -c "
import sys; sys.path.insert(0, 'gateway')
import db
info = db.validate_superuser_key('limited-dashboards')
print(f'Dashboards: {info[\"dashboards\"]}')
print(f'Can access polymarket: {db.has_superuser_key_access(\"limited-dashboards\", \"polymarket\")}')
print(f'Can access climate: {db.has_superuser_key_access(\"limited-dashboards\", \"climate\")}')
"
```

### Scenario 3: Aspect-Based Restrictions

```bash
# 1. Create read-only key
custom_key: read-only-investor
name: Read Only Investor
aspects: read-only,demo-mode

# 2. Check aspects in header
curl -i -H "Authorization: Bearer read-only-investor" http://localhost:7000/ | grep "X-Gateway-Key-Aspects"
# Expected: X-Gateway-Key-Aspects: read-only,demo-mode

# 3. Dashboard can check and disable write features
# (See Dashboard Integration section below)
```

### Scenario 4: Key Expiration

```bash
# 1. Create key that expires in 1 second (for testing)
custom_key: expiring-key
expires_in_days: 0  # Actually use 1 second via direct DB

# 2. Or manually in DB:
python3 -c "
import sys, time; sys.path.insert(0, 'gateway')
import db
key = db.create_superuser_key(
    name='Expiring Now',
    custom_key='expired-test',
    expires_in_days=None
)
# Manually expire it
import sqlite3
conn = sqlite3.connect('gateway/auth.db')
conn.execute('UPDATE superuser_keys SET expires_at = ? WHERE key = ?', 
             (int(time.time()) - 1, 'expired-test'))
conn.commit()
print('Key created and set to expired')
"

# 3. Test accessing with expired key
curl -H "Authorization: Bearer expired-test" http://localhost:7000/
# Expected: 302 redirect (access denied)
```

### Scenario 5: Enable/Disable Toggle

```bash
# 1. Get key ID
python3 -c "
import sys; sys.path.insert(0, 'gateway')
import db
keys = db.list_superuser_keys()
for k in keys:
    if k['name'] == 'Test Key':
        print(f'ID: {k[\"id\"]}, Active: {k[\"active\"]}')
"

# 2. Test with active key
curl -H "Authorization: Bearer test-key" http://localhost:7000/
# Expected: 200 OK

# 3. Disable via admin
curl -X POST http://localhost:7000/admin/superuser-keys/1/disable \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "_csrf_token=YOUR_CSRF_TOKEN"

# 4. Test with disabled key
curl -H "Authorization: Bearer test-key" http://localhost:7000/
# Expected: 302 redirect (access denied)

# 5. Re-enable
curl -X POST http://localhost:7000/admin/superuser-keys/1/enable \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "_csrf_token=YOUR_CSRF_TOKEN"

# 6. Test again
curl -H "Authorization: Bearer test-key" http://localhost:7000/
# Expected: 200 OK again
```

### Scenario 6: Custom Aspects

```bash
# 1. Create key with custom aspect
custom_key: partner-demo
aspects: partner-demo,view-only

# 2. Check in code
python3 -c "
import sys; sys.path.insert(0, 'gateway')
import db
has_aspect = db.key_has_aspect('partner-demo', 'partner-demo')
print(f'Has partner-demo aspect: {has_aspect}')
aspects = db.get_key_aspects('partner-demo')
print(f'All aspects: {aspects}')
"
```

## Unit Tests (Python)

```python
# tests/test_superuser_keys.py
import sys
import time
sys.path.insert(0, 'gateway')
import db

def test_create_key():
    """Test basic key creation."""
    key = db.create_superuser_key(
        name="Test Key",
        custom_key="test-123",
        dashboards=["polymarket"],
        aspects=["read-only"],
        expires_in_days=7
    )
    assert key == "test-123"
    info = db.validate_superuser_key("test-123")
    assert info is not None
    assert info["name"] == "Test Key"
    assert "polymarket" in info["dashboards"]
    assert "read-only" in info["aspects"]
    print("✓ test_create_key passed")

def test_custom_key_validation():
    """Test custom key validation."""
    # Valid keys
    db.create_superuser_key("Valid 1", custom_key="valid-key-123")
    db.create_superuser_key("Valid 2", custom_key="valid_key_123")
    
    # Invalid keys
    try:
        db.create_superuser_key("Invalid", custom_key="ab")  # Too short
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "3 characters" in str(e)
    
    try:
        db.create_superuser_key("Invalid", custom_key="key@invalid")  # Bad char
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "alphanumeric" in str(e)
    
    print("✓ test_custom_key_validation passed")

def test_key_expiration():
    """Test key expiration."""
    # Create already-expired key
    import sqlite3
    key = db.create_superuser_key("Expired", custom_key="expired-test")
    conn = sqlite3.connect("gateway/auth.db")
    conn.execute("UPDATE superuser_keys SET expires_at = ? WHERE key = ?",
                 (int(time.time()) - 1, "expired-test"))
    conn.commit()
    
    # Validate should return None for expired
    info = db.validate_superuser_key("expired-test")
    assert info is None
    print("✓ test_key_expiration passed")

def test_dashboard_restrictions():
    """Test dashboard access restrictions."""
    db.create_superuser_key(
        "Limited",
        custom_key="limited",
        dashboards=["polymarket", "crypto"]
    )
    
    assert db.has_superuser_key_access("limited", "polymarket")
    assert db.has_superuser_key_access("limited", "crypto")
    assert not db.has_superuser_key_access("limited", "climate")
    print("✓ test_dashboard_restrictions passed")

def test_aspect_checking():
    """Test aspect checking."""
    db.create_superuser_key(
        "Aspects",
        custom_key="aspects-test",
        aspects=["read-only", "demo-mode"]
    )
    
    assert db.key_has_aspect("aspects-test", "read-only")
    assert db.key_has_aspect("aspects-test", "demo-mode")
    assert not db.key_has_aspect("aspects-test", "vip")
    
    aspects = db.get_key_aspects("aspects-test")
    assert len(aspects) == 2
    print("✓ test_aspect_checking passed")

def test_enable_disable():
    """Test enabling/disabling keys."""
    db.create_superuser_key("Toggle", custom_key="toggle-test")
    
    # Initially active
    assert db.validate_superuser_key("toggle-test") is not None
    
    # Disable
    db.disable_superuser_key(1)  # Assumes ID 1
    assert db.validate_superuser_key("toggle-test") is None
    
    # Enable
    db.enable_superuser_key(1)
    assert db.validate_superuser_key("toggle-test") is not None
    print("✓ test_enable_disable passed")

if __name__ == "__main__":
    test_create_key()
    test_custom_key_validation()
    test_key_expiration()
    test_dashboard_restrictions()
    test_aspect_checking()
    test_enable_disable()
    print("\n✓ All tests passed!")
```

Run tests:
```bash
cd /home/user/Habbig
python3 tests/test_superuser_keys.py
```

## Integration Tests

### Test Admin Dashboard UI

1. Navigate to `http://localhost:7000/admin`
2. Click "Investor Keys" tab
3. Fill in form:
   - Custom Key: `ui-test-key`
   - Name: `UI Test Key`
   - Dashboards: `polymarket,crypto`
   - Aspects: `read-only`
   - Expiration: `7 days`
4. Click "Create Key"
5. Verify success banner shows key
6. Verify key appears in list
7. Test disable/enable buttons
8. Test delete button

### Test API Endpoints

```bash
# Test JSON API for listing keys
curl -H "Cookie: pm_gateway_session=ADMIN_SESSION" \
  http://localhost:7000/admin/api/superuser-keys | jq

# Expected output:
# {
#   "keys": [
#     {
#       "id": 1,
#       "name": "Test Key",
#       "dashboards": ["polymarket"],
#       "aspects": ["read-only"],
#       "active": true,
#       "created_at": 1234567890,
#       "expires_at": 1234567890,
#       "last_used_at": null
#     }
#   ]
# }
```

## Performance Testing

### Measure Key Validation Speed

```bash
# Time 1000 validations
python3 -c "
import sys, time; sys.path.insert(0, 'gateway')
import db

# Create test key
db.create_superuser_key('Perf Test', custom_key='perf-test')

# Measure validation time
start = time.time()
for _ in range(1000):
    db.validate_superuser_key('perf-test')
elapsed = time.time() - start

print(f'1000 validations: {elapsed:.3f}s ({1000/elapsed:.0f} ops/sec)')
"
```

Expected: ~1000+ ops/sec (very fast due to caching)

## Debugging

### Check Database State

```bash
python3 << 'EOF'
import sys
sys.path.insert(0, 'gateway')
import db

# List all keys
keys = db.list_superuser_keys()
for k in keys:
    print(f"ID: {k['id']}, Name: {k['name']}, Active: {k['active']}")

# Check specific key
info = db.validate_superuser_key('test-key')
if info:
    print(f"Key found: {info}")
else:
    print("Key not found or expired")
EOF
```

### Check Gateway Logs

```bash
# Tail gateway logs
tail -f /tmp/habbig-gateway.log | grep -i "superuser\|investor"
```

### Check Request Headers

```bash
# Use curl -v to see all headers
curl -v -H "Authorization: Bearer test-key" http://localhost:7000/ 2>&1 | grep "X-Gateway"
```

---

**Ready to test?** Start with Scenario 1, then work through the others!
