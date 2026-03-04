import Foundation
import MapKit

@MainActor
final class MountainMapViewModel: ObservableObject {
    @Published var resort: Resort
    @Published var terrainState: TerrainState
    @Published var playerTrail: PlayerTrail
    @Published var friendTrails: [PlayerTrail]
    @Published var hudState: HUDState
    @Published var selectedEvent: MapEvent?
    @Published var isMiniMapCollapsed = false

    private let weatherService: WeatherSyncing
    private let gpsService: GPSRunTracking
    private let socialService: SocialSyncing

    init(
        resort: Resort,
        terrainState: TerrainState,
        playerTrail: PlayerTrail,
        friendTrails: [PlayerTrail],
        hudState: HUDState,
        weatherService: WeatherSyncing,
        gpsService: GPSRunTracking,
        socialService: SocialSyncing
    ) {
        self.resort = resort
        self.terrainState = terrainState
        self.playerTrail = playerTrail
        self.friendTrails = friendTrails
        self.hudState = hudState
        self.weatherService = weatherService
        self.gpsService = gpsService
        self.socialService = socialService
    }

    func refreshLiveLayers() async {
        async let terrain = weatherService.latestTerrainState(for: resort)
        async let trail = gpsService.latestTrail()
        async let friends = socialService.friendTrails()

        terrainState = await terrain
        playerTrail = await trail
        friendTrails = await friends
        hudState = Self.calculateHUD(from: playerTrail, resort: resort)
    }

    func completeSelectedEvent() {
        guard let eventID = selectedEvent?.id else { return }
        if let index = resort.events.firstIndex(where: { $0.id == eventID }) {
            resort.events[index].isCompleted = true
            selectedEvent = resort.events[index]
        }
    }

    private static func calculateHUD(from trail: PlayerTrail, resort: Resort) -> HUDState {
        let verticalMeters = Int(Double(trail.points.count) * 24)
        let combo = max(1, trail.points.count / 8)
        let progress = Double(resort.events.filter(\.isCompleted).count) / Double(max(resort.events.count, 1))

        return HUDState(
            zoneName: resort.sectors.first(where: \.isUnlocked)?.name ?? "Locked Sector",
            xpTick: "+50 vertical, chain x\(combo)",
            festivalProgress: progress,
            verticalMeters: verticalMeters,
            comboMultiplier: combo
        )
    }
}

extension MountainMapViewModel {
    static var livePreview: MountainMapViewModel {
        let center = CLLocationCoordinate2D(latitude: 46.5884, longitude: 11.7843)
        let resort = Resort(
            id: UUID(),
            name: "Dolomiti Forza Slopes",
            center: center,
            sectors: [
                Sector(id: UUID(), name: "Cruiser Zone", difficulty: .cruiser, boundary: [], unlockBand: 1, isUnlocked: true, heatScore: 0.33),
                Sector(id: UUID(), name: "Adventurer Zone", difficulty: .adventurer, boundary: [], unlockBand: 2, isUnlocked: true, heatScore: 0.76),
                Sector(id: UUID(), name: "Expert Zone", difficulty: .expert, boundary: [], unlockBand: 3, isUnlocked: false, heatScore: 0.91)
            ],
            events: [
                MapEvent(id: UUID(), title: "Liftline Sprint", type: .gate, coordinate: center, difficulty: 2, reward: .init(xp: 250, badge: nil), isCompleted: false),
                MapEvent(id: UUID(), title: "First To Summit", type: .accolade, coordinate: .init(latitude: 46.592, longitude: 11.79), difficulty: 4, reward: .init(xp: 600, badge: "Peak Crown"), isCompleted: false),
                MapEvent(id: UUID(), title: "Powder Hunt", type: .playlist, coordinate: .init(latitude: 46.5845, longitude: 11.779), difficulty: 3, reward: .init(xp: 450, badge: "Tree Hunter"), isCompleted: true)
            ]
        )

        let emptyTerrain = TerrainState(snowDepthCM: 122, visibility: .clear, groomedRatio: 0.58, updatedAt: .now)
        let emptyTrail = PlayerTrail(id: UUID(), ownerName: "You", isFriendTrail: false, points: [])
        let hud = HUDState(zoneName: "Cruiser Zone", xpTick: "+50 vertical, chain x3", festivalProgress: 0.33, verticalMeters: 1200, comboMultiplier: 3)

        return MountainMapViewModel(
            resort: resort,
            terrainState: emptyTerrain,
            playerTrail: emptyTrail,
            friendTrails: [],
            hudState: hud,
            weatherService: MockWeatherService(),
            gpsService: MockGPSService(seed: center),
            socialService: MockSocialService(seed: center)
        )
    }
}
