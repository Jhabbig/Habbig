# Dashboard Aspect Integration Guide

## Overview

When an investor accesses a dashboard using a superuser key, the gateway sends headers indicating:
1. **Investor Mode** - They're using a key, not a regular account
2. **Available Aspects** - Permission/feature flags that customize their experience

This guide explains how to integrate aspect checking into your dashboards.

## Headers Sent by Gateway

When a superuser key is used, you'll receive:

```
X-Gateway-Investor-Mode: true
X-Gateway-Key-Aspects: read-only,demo-mode,limited-data
X-Gateway-User-Id: superuser
X-Gateway-User-Email: investor@dashboard
```

The `X-Gateway-Key-Aspects` header contains comma-separated aspect strings.

## Backend Integration (Python/FastAPI)

### 1. Extract Aspects from Request

```python
from fastapi import Request

async def get_investor_aspects(request: Request) -> list[str]:
    """Extract aspects from X-Gateway-Key-Aspects header."""
    aspects_header = request.headers.get("X-Gateway-Key-Aspects", "")
    return [a.strip() for a in aspects_header.split(",") if a.strip()]

async def is_investor_mode(request: Request) -> bool:
    """Check if user is in investor mode."""
    return request.headers.get("X-Gateway-Investor-Mode") == "true"

async def has_aspect(request: Request, aspect: str) -> bool:
    """Check if investor has a specific aspect."""
    aspects = await get_investor_aspects(request)
    return aspect.lower() in [a.lower() for a in aspects]
```

### 2. Route Protection

```python
from fastapi import HTTPException, status

@app.post("/api/trading/place")
async def place_trade(request: Request, trade_data: TradeRequest):
    """Place a trade - blocked for read-only investors."""
    if await has_aspect(request, "read-only"):
        raise HTTPException(
            status_code=403,
            detail="Read-only access - trading not allowed"
        )
    
    if await has_aspect(request, "no-trading"):
        raise HTTPException(
            status_code=403,
            detail="Your investor key doesn't have trading permissions"
        )
    
    # Process trade...
    return {"status": "success"}

@app.get("/api/data/export")
async def export_data(request: Request):
    """Export data - blocked if no-export aspect."""
    if await has_aspect(request, "no-export"):
        raise HTTPException(
            status_code=403,
            detail="Data export is not allowed with your key"
        )
    
    # Export...
    return {"data": [...]}
```

### 3. Data Filtering

```python
@app.get("/api/portfolio")
async def get_portfolio(request: Request, user: dict):
    """Return portfolio - filter based on aspects."""
    portfolio = get_user_portfolio(user["user_id"])
    
    # Limited data aspect - remove sensitive info
    if await has_aspect(request, "limited-data"):
        portfolio = {
            "summary": portfolio["summary"],
            "positions_count": len(portfolio["positions"]),
            # Don't include detailed positions
        }
    
    # Demo mode - use demo data
    if await has_aspect(request, "demo-mode"):
        portfolio = get_demo_portfolio()
    
    return portfolio

@app.get("/api/analytics")
async def get_analytics(request: Request):
    """Return analytics - limited if requested."""
    data = calculate_analytics()
    
    if await has_aspect(request, "view-only-summary"):
        # Only high-level summary
        return {
            "total_volume": data["total_volume"],
            "roi": data["roi"],
            "win_rate": data["win_rate"],
        }
    
    if await has_aspect(request, "limited-data"):
        # Remove 30-day and real-time data
        return {k: v for k, v in data.items() 
                if "30d" not in k and "realtime" not in k}
    
    return data
```

### 4. Audit Logging

```python
import logging

logger = logging.getLogger("investor_access")

@app.get("/api/sensitive-data")
async def get_sensitive(request: Request):
    """Log investor access to sensitive data."""
    aspects = await get_investor_aspects(request)
    
    logger.info(
        "Investor accessed sensitive data",
        extra={
            "user_id": request.headers.get("X-Gateway-User-Id"),
            "email": request.headers.get("X-Gateway-User-Email"),
            "aspects": aspects,
            "path": request.url.path,
        }
    )
    
    if await has_aspect(request, "audit-enabled"):
        # Store detailed audit log
        store_audit_log(
            user="investor@dashboard",
            action="access_sensitive_data",
            aspects=aspects,
            timestamp=datetime.now()
        )
    
    return get_sensitive_data()
```

## Frontend Integration (React/JavaScript)

### 1. Extract Aspects in Browser

```javascript
// Get aspects from X-Gateway-Key-Aspects header
// Note: Headers are not accessible in browser JavaScript for security reasons
// Instead, get them from the server via an API endpoint

async function getInvestorAspects() {
  const response = await fetch('/api/auth/aspects');
  const data = await response.json();
  return data.aspects; // ['read-only', 'demo-mode']
}

async function hasAspect(aspect) {
  const aspects = await getInvestorAspects();
  return aspects.includes(aspect.toLowerCase());
}

async function isInvestorMode() {
  const response = await fetch('/api/auth/is-investor');
  const data = await response.json();
  return data.is_investor;
}
```

### 2. Server Endpoint to Get Aspects

```python
@app.get("/api/auth/aspects")
async def get_aspects(request: Request):
    """Return investor aspects for frontend."""
    aspects = await get_investor_aspects(request)
    return {"aspects": aspects}

@app.get("/api/auth/is-investor")
async def is_investor(request: Request):
    """Check if user is in investor mode."""
    return {"is_investor": await is_investor_mode(request)}
```

### 3. UI Component to Hide/Show Features

```javascript
// React component that respects investor aspects
import { useEffect, useState } from 'react';

function TradeButton({ onTrade }) {
  const [isReadOnly, setIsReadOnly] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    getInvestorAspects().then(aspects => {
      setIsReadOnly(
        aspects.includes('read-only') || 
        aspects.includes('no-trading')
      );
      setIsLoading(false);
    });
  }, []);

  if (isLoading) return <div>Loading...</div>;

  if (isReadOnly) {
    return (
      <button disabled title="Trading not allowed with read-only access">
        Place Trade (disabled)
      </button>
    );
  }

  return (
    <button onClick={onTrade}>
      Place Trade
    </button>
  );
}

// Usage
<TradeButton onTrade={() => placeTrade()} />
```

### 4. Feature Gating

```javascript
// Show/hide UI sections based on aspects
async function renderUI() {
  const aspects = await getInvestorAspects();

  // Hide export button for no-export aspect
  if (aspects.includes('no-export')) {
    document.getElementById('export-btn').style.display = 'none';
  }

  // Show demo banner for demo-mode
  if (aspects.includes('demo-mode')) {
    document.getElementById('demo-banner').style.display = 'block';
    document.getElementById('demo-banner').textContent = 
      '⚠️ This is demo data. Not real trading data.';
  }

  // Show limited-data warning
  if (aspects.includes('limited-data')) {
    document.getElementById('data-warning').style.display = 'block';
    document.getElementById('data-warning').textContent = 
      '⚠️ You have access to limited data. Contact support for full access.';
  }

  // Show trial status
  if (aspects.includes('trial')) {
    const daysLeft = calculateTrialDaysLeft();
    document.getElementById('trial-badge').textContent = 
      `Trial (${daysLeft} days left)`;
  }
}

renderUI();
```

## Common Aspect Patterns

### Pattern 1: Read-Only View

```python
@app.post("/api/any-mutation")
async def mutation(request: Request, data: dict):
    """Prevent mutations for read-only users."""
    if await has_aspect(request, "read-only"):
        return HTTPException(403, "This action requires write access")
    
    # Process mutation...
```

### Pattern 2: Demo Mode

```python
async def get_portfolio_data(request: Request, user: dict):
    """Return real or demo data based on aspect."""
    if await has_aspect(request, "demo-mode"):
        return {
            "status": "demo",
            "data": get_demo_portfolio(),
            "note": "This is sample data for demonstration"
        }
    
    return {
        "status": "real",
        "data": get_real_portfolio(user),
    }
```

### Pattern 3: Limited Data Access

```python
async def get_analytics(request: Request):
    """Different data depth based on aspect."""
    full_analytics = calculate_full_analytics()
    
    if await has_aspect(request, "limited-data"):
        # Return only summary
        return {
            "total_volume": full_analytics["total"],
            "pnl": full_analytics["pnl"],
            # Remove: detailed positions, real-time updates, etc.
        }
    
    # Full detailed analytics
    return full_analytics
```

### Pattern 4: Audit Logging

```python
async def log_investor_action(request: Request, action: str, details: dict = None):
    """Log actions by investors."""
    aspects = await get_investor_aspects(request)
    
    if "audit-enabled" in aspects:
        audit_db.insert({
            "timestamp": datetime.now(),
            "user": request.headers.get("X-Gateway-User-Email"),
            "action": action,
            "details": details,
            "aspects": aspects,
        })
```

## Testing Aspects Locally

### 1. Mock Headers in Development

```python
from fastapi import Request
from unittest.mock import MagicMock

# Create mock request with aspects
mock_request = MagicMock(spec=Request)
mock_request.headers = {
    "X-Gateway-Investor-Mode": "true",
    "X-Gateway-Key-Aspects": "read-only,demo-mode"
}

# Test your function
result = await your_function(mock_request)
assert result["error"] == "read-only access not allowed"
```

### 2. Test with Real Key

```bash
# Start your dashboard
python3 -m uvicorn main:app --reload

# Make request with test key
curl -H "X-Gateway-Investor-Mode: true" \
     -H "X-Gateway-Key-Aspects: read-only" \
     http://localhost:8000/api/data

# Should restrict based on read-only aspect
```

## Best Practices

### DO:

✅ **Check aspects on the backend** - Never trust client-side aspect checks alone  
✅ **Log investor access** - Help with compliance and debugging  
✅ **Provide clear error messages** - Let investors know why something is blocked  
✅ **Test with different aspect combinations** - read-only + demo-mode, etc.  
✅ **Document restrictions** - Tell investors what aspects limit  
✅ **Use consistent aspect names** - Coordination across dashboards  

### DON'T:

❌ **Only check aspects in frontend** - Can be bypassed  
❌ **Ignore security headers** - Trust X-Gateway-* headers for critical decisions  
❌ **Create too many aspects** - Keep it simple (5-10 main ones)  
❌ **Change aspect behavior** - Maintain consistency for investors  
❌ **Forget to handle missing aspects** - Default to most restrictive  

## Example: Complete Dashboard Integration

```python
from fastapi import FastAPI, Request, HTTPException
from datetime import datetime
import logging

app = FastAPI()
logger = logging.getLogger(__name__)

# Utility functions
async def get_aspects(request: Request) -> list[str]:
    header = request.headers.get("X-Gateway-Key-Aspects", "")
    return [a.strip() for a in header.split(",") if a.strip()]

async def check_aspect(request: Request, required_aspect: str, deny: bool = False):
    """Check if investor has/doesn't have aspect."""
    aspects = await get_aspects(request)
    if deny and required_aspect in aspects:
        raise HTTPException(403, f"{required_aspect} is not allowed")
    if not deny and required_aspect not in aspects:
        raise HTTPException(403, f"Requires {required_aspect} aspect")

# Protected endpoints
@app.get("/api/portfolio")
async def get_portfolio(request: Request):
    aspects = await get_aspects(request)
    portfolio = get_user_portfolio()
    
    # Apply restrictions
    if "limited-data" in aspects:
        portfolio = filter_sensitive_data(portfolio)
    if "demo-mode" in aspects:
        portfolio = get_demo_portfolio()
    
    return portfolio

@app.post("/api/trade")
async def place_trade(request: Request, trade: dict):
    await check_aspect(request, "read-only", deny=True)
    await check_aspect(request, "no-trading", deny=True)
    
    logger.info(f"Trade placed: {trade}")
    return {"status": "success"}

@app.get("/api/export")
async def export_data(request: Request):
    await check_aspect(request, "no-export", deny=True)
    
    data = get_export_data()
    return data

# Ready to go! Test with:
# curl -H "X-Gateway-Key-Aspects: read-only" http://localhost:8000/api/trade
```

---

**Questions?** Check the examples or contact the Habbig team!
