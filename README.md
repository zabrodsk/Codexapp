# SkiHUD (Forza on Slopes) iOS MVP

SkiHUD is a **Forza Horizon-inspired mountain map HUD** for skiing/snowboarding. The map is the heart of the app: it turns a static piste map into a live game layer showing terrain state, friend trails, progression sectors, events, and moment-to-moment XP feedback.

## Product plan

### 1) Core map loop (MVP)
- High-fidelity MapKit base with 3D/hybrid terrain.
- Real-time overlays:
  - Your trail as a live route line.
  - Friends as ghost-like dashed trails.
  - Event markers (gate/accolade/playlist/social).
- Tap any marker for challenge metadata and instant completion flow.

### 2) Progression and world unlocks
- Resort partitioned into Forza-like sectors:
  - **Cruiser Zone** (Band 1)
  - **Adventurer Zone** (Band 2)
  - **Expert Zone** (Band 3)
- Sector unlocks are represented by `unlockBand` + `isUnlocked` and can be driven by XP/event completion.

### 3) Dynamic terrain states
- Terrain panel is fed by a weather/crowd sync layer:
  - Snow depth
  - Visibility (clear/foggy)
  - Groomed ratio
- Current implementation uses a mocked weather actor for deterministic simulation.

### 4) Live tracking HUD
- Collapsible HUD card with:
  - Current zone name
  - XP chain ticker
  - Festival progress bar
  - Vertical meters + combo multiplier

## Implemented code structure

```
SkiHUD/
  App/
    SkiHUDApp.swift
  Models/
    DomainModels.swift
  Services/
    SimulationServices.swift
  ViewModels/
    MountainMapViewModel.swift
  Views/
    MountainMapScreen.swift
    HUDComponents.swift
```

### Architecture
- **SwiftUI + MapKit** UI with MVVM.
- **Actor-based service protocols** for async data sync:
  - `WeatherSyncing`
  - `GPSRunTracking`
  - `SocialSyncing`
- `MountainMapViewModel` orchestrates live refresh and computes HUD progression state.

## Next build steps (production)
1. Replace mock services with:
   - CoreLocation + CoreMotion run detector.
   - Firebase/Firestore live friend sync.
   - Weather + resort/lift APIs.
2. Add map tile overlays for ski runs/lifts (OpenSkiMap or resort vector tiles).
3. Add offline GPX cache for poor-connectivity lifts.
4. Ship gameplay systems:
   - Sector unlock economy.
   - Seasonal playlist rotation.
   - Leaderboards and ghost replay packets.

## How to run
1. Create a new iOS SwiftUI project in Xcode named `SkiHUD`.
2. Copy the files from this repository into the project (preserving folder layout).
3. Ensure deployment target is iOS 17+ (for modern SwiftUI `Map`).
4. Build and run on simulator/device.

