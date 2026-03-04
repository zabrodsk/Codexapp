import Foundation
import CoreLocation

protocol WeatherSyncing {
    func latestTerrainState(for resort: Resort) async -> TerrainState
}

protocol GPSRunTracking {
    func beginRun() async
    func latestTrail() async -> PlayerTrail
}

protocol SocialSyncing {
    func friendTrails() async -> [PlayerTrail]
}

actor MockWeatherService: WeatherSyncing {
    func latestTerrainState(for resort: Resort) async -> TerrainState {
        let minute = Calendar.current.component(.minute, from: .now)
        let visibility: TerrainVisibility = (minute % 3 == 0) ? .foggy : .clear
        return TerrainState(
            snowDepthCM: 115 + Double(minute % 12),
            visibility: visibility,
            groomedRatio: 0.45 + Double((minute % 5)) * 0.1,
            updatedAt: .now
        )
    }
}

actor MockGPSService: GPSRunTracking {
    private var points: [TrailPoint] = []
    private let seed: CLLocationCoordinate2D

    init(seed: CLLocationCoordinate2D) {
        self.seed = seed
    }

    func beginRun() async {
        points.removeAll()
        for step in 0..<28 {
            points.append(
                TrailPoint(
                    id: UUID(),
                    coordinate: CLLocationCoordinate2D(
                        latitude: seed.latitude + Double(step) * 0.00027,
                        longitude: seed.longitude + Double(step) * 0.00009
                    ),
                    timestamp: .now.addingTimeInterval(Double(step) * -14),
                    speedMPS: Double(5 + (step % 12)),
                    flowScore: Double(55 + (step % 45))
                )
            )
        }
    }

    func latestTrail() async -> PlayerTrail {
        if points.isEmpty {
            await beginRun()
        }

        return PlayerTrail(
            id: UUID(),
            ownerName: "You",
            isFriendTrail: false,
            points: points
        )
    }
}

actor MockSocialService: SocialSyncing {
    private let seed: CLLocationCoordinate2D

    init(seed: CLLocationCoordinate2D) {
        self.seed = seed
    }

    func friendTrails() async -> [PlayerTrail] {
        let names = ["Alex", "Mika", "Jules"]
        return names.enumerated().map { index, name in
            let points = (0..<14).map { offset in
                TrailPoint(
                    id: UUID(),
                    coordinate: CLLocationCoordinate2D(
                        latitude: seed.latitude + Double(index) * 0.0016 + Double(offset) * 0.00019,
                        longitude: seed.longitude - Double(index) * 0.0014 + Double(offset) * 0.00011
                    ),
                    timestamp: .now.addingTimeInterval(Double(offset) * -22),
                    speedMPS: Double(7 + (offset % 7)),
                    flowScore: Double(48 + offset)
                )
            }

            return PlayerTrail(
                id: UUID(),
                ownerName: name,
                isFriendTrail: true,
                points: points
            )
        }
    }
}
