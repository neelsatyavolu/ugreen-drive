import AppKit
import SwiftUI

/// "Machine & Ink" (same tokens as the Drive web app, DESIGN.md): warm
/// ink-black canvas with warm off-white text (a light "Paper" variant), one
/// blue accent for small marks, Geist for UI copy, Geist Mono for headings,
/// labels and data, flat surfaces and square corners.
enum Brand {
    static let radius: CGFloat = 0

    /// Primary buttons: text-colored fill with canvas-colored text.
    static let fill = Color.dynamic(dark: .hex(0xF2F0EA), light: .hex(0x16161A))
    static let fillHover = Color.dynamic(dark: .hex(0xF2F0EA, alpha: 0.86), light: .hex(0x16161A, alpha: 0.86))
    static let onBrand = Color.dynamic(dark: .hex(0x0E0E0D), light: .hex(0xF7F5EF))
    static let accent = Color.dynamic(dark: .hex(0x4C8DFF), light: .hex(0x2F6FE0))
    static let accentTint = Color.dynamic(dark: .hex(0x4C8DFF, alpha: 0.12), light: .hex(0x2F6FE0, alpha: 0.10))
    static let danger = Color.dynamic(dark: .hex(0xE5483A), light: .hex(0xC8352A))
    static let dangerTint = Color.dynamic(dark: .hex(0xE5483A, alpha: 0.12), light: .hex(0xC8352A, alpha: 0.08))

    static let background = Color.dynamic(dark: .hex(0x0E0E0D), light: .hex(0xF7F5EF))
    static let card = Color.dynamic(dark: .hex(0x161615), light: .hex(0xFCFBF7))
    static let secondary = Color.dynamic(dark: .hex(0x1F1F1D), light: .hex(0xEEECE5))
    static let border = Color.dynamic(dark: .hex(0xF2F0EA, alpha: 0.10), light: .hex(0x16161A, alpha: 0.12))
    static let foreground = Color.dynamic(dark: .hex(0xF2F0EA), light: .hex(0x16161A))
    static let muted = Color.dynamic(dark: .hex(0xF2F0EA, alpha: 0.68), light: .hex(0x16161A, alpha: 0.66))

    /// Registers the bundled Geist and Geist Mono (SIL OFL) for this process.
    static func registerFonts() {
        for name in ["Geist", "GeistMono"] {
            if let url = Bundle.main.url(forResource: name, withExtension: "ttf", subdirectory: "Fonts") {
                CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
            }
        }
    }
}

extension Font {
    /// Geist for buttons, forms and UI copy.
    static func sans(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .custom("Geist", size: size).weight(weight)
    }

    /// Geist Mono for headings, labels, the wordmark and data (sizes, speeds, paths).
    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .custom("Geist Mono", size: size).weight(weight)
    }
}

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        self.init(nsColor: .hex(hex, alpha: alpha))
    }

    static func dynamic(dark: NSColor, light: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        })
    }
}

extension NSColor {
    static func hex(_ value: UInt32, alpha: Double = 1) -> NSColor {
        NSColor(srgbRed: CGFloat((value >> 16) & 0xFF) / 255,
                green: CGFloat((value >> 8) & 0xFF) / 255,
                blue: CGFloat(value & 0xFF) / 255,
                alpha: alpha)
    }
}

/// Solid text-colored button with canvas-colored text, square corners.
struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.sans(13, .semibold))
            .foregroundStyle(Brand.onBrand)
            .frame(maxWidth: .infinity, minHeight: 32)
            .background(configuration.isPressed ? Brand.fillHover : Brand.fill, in: Rectangle())
            .opacity(isEnabled ? 1 : 0.45)
            .contentShape(Rectangle())
    }
}

/// Quiet outlined button.
struct SecondaryButtonStyle: ButtonStyle {
    var tint: Color = Brand.foreground

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.sans(13, .medium))
            .foregroundStyle(tint)
            .frame(maxWidth: .infinity, minHeight: 32)
            .background(configuration.isPressed ? Brand.secondary : Color.clear, in: Rectangle())
            .overlay(Rectangle().strokeBorder(Brand.border))
            .contentShape(Rectangle())
    }
}

/// Small text-like button for footers and section headers.
struct LinkButtonStyle: ButtonStyle {
    var tint: Color = Brand.muted

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.sans(12, .medium))
            .foregroundStyle(configuration.isPressed ? Brand.foreground : tint)
            .contentShape(Rectangle())
    }
}

/// Uppercase Geist Mono eyebrow with 0.08em tracking.
struct SectionLabel: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(.mono(11, .medium))
            .tracking(0.9)
            .foregroundStyle(Brand.muted)
    }
}

/// The wordmark: a small accent square, then the Drive's name in Geist Mono caps.
struct Wordmark: View {
    let name: String
    var size: CGFloat = 14

    var body: some View {
        HStack(spacing: size * 0.6) {
            Rectangle().fill(Brand.accent).frame(width: size * 0.75, height: size * 0.75)
            Text(name.uppercased())
                .font(.mono(size, .semibold))
                .tracking(size * 0.06)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(name)
    }
}
