# CLAUDE.md — Project Context

## Mission-Critical Context

This is an **emergency evacuation flight tracker** for the Gulf region during an active war crisis. Real people are using this tool to find flights out of Dubai, Doha, Riyadh, and other GCC airports.

**Every decision must prioritize:**
1. **Reliability over features** — A broken feature costs lives. Degrade gracefully. Always show cached/stale data over nothing.
2. **Speed of information** — People need to see available flights fast. Minimize loading states. Show partial results immediately.
3. **Accuracy** — Wrong information (e.g., showing sold-out flights as available) wastes precious time. When uncertain, say so clearly.
4. **Accessibility** — Users may be stressed, on poor connections, on mobile. Keep the UI simple and readable.

## Architecture

- **Frontend:** Vanilla JS single-page app (`public/index.html`, `public/style.css`)
- **Backend:** Flask API deployed on Vercel serverless (`api/`)
- **Data sources:** FlightRadar24 (flight data), Amadeus API (seat availability)
- **Caching:** 2-minute TTL on flight data, 10-minute TTL on availability, all with stale fallback

## Key Design Decisions

- Show only flights with confirmed seat availability (when Amadeus is configured)
- Auto-refresh flight data and availability every 2 minutes
- Airline "Book Now" links pre-fill origin, destination, date, and passenger count
- All API calls have retry logic with exponential backoff
- Failed airports never block other airports from loading
- Non-JSON server errors are handled gracefully (safeJson helper)

## Development Rules

- Never ship a change that could break the page for someone mid-evacuation
- Always preserve cached/stale data fallback paths
- Test error handling paths — they WILL be hit under load
- Keep dependencies minimal — fewer things to break
- No unnecessary abstractions — this is emergency software, not a framework
