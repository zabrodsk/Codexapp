import Foundation
import CoreLocation
import SwiftUI

struct Resort: Identifiable {
    let id: UUID
    var name: String
    var center: CLLocationCoordinate2D
    var sectors: [Sector]
    var events: [MapEvent]
}

enum SectorDifficulty: String, CaseIterable {
    case cruiser
    case adventurer
    case expert
}

struct Sector: Identifiable {
    let id: UUID
    var name: String
    var difficulty: SectorDifficulty
    var boundary: [CLLocationCoordinate2D]
    var unlockBand: Int
    var isUnlocked: Bool
    var heatScore: Double
}

enum TerrainVisibility: String {
    case clear
    case foggy
}

struct TerrainState {
    var snowDepthCM: Double
    var visibility: TerrainVisibility
    var groomedRatio: Double
    var updatedAt: Date
}

struct TrailPoint: Identifiable {
    let id: UUID
    var coordinate: CLLocationCoordinate2D
    var timestamp: Date
    var speedMPS: Double
    var flowScore: Double
}

struct PlayerTrail: Identifiable {
    let id: UUID
    var ownerName: String
    var isFriendTrail: Bool
    var points: [TrailPoint]
}

enum EventType: String {
    case gate
    case accolade
    case playlist
    case social
}

struct EventReward {
    var xp: Int
    var badge: String?
}

struct MapEvent: Identifiable {
    let id: UUID
    var title: String
    var type: EventType
    var coordinate: CLLocationCoordinate2D
    var difficulty: Int
    var reward: EventReward
    var isCompleted: Bool
}

struct HUDState {
    var zoneName: String
    var xpTick: String
    var festivalProgress: Double
    var verticalMeters: Int
    var comboMultiplier: Int
}

extension Color {
    static func speedColor(for speedMPS: Double) -> Color {
        switch speedMPS {
        case ..<6: return .mint
        case 6..<11: return .cyan
        case 11..<18: return .orange
        default: return .pink
        }
    }
}
