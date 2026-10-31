import Foundation
import Vision
import ImageIO

struct Word: Codable {
    let text: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let confidence: Double
}

do {
    guard CommandLine.arguments.count == 2,
          let source = CGImageSourceCreateWithURL(
            URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "OCR", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "Cannot open OCR image"])
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0.001
    try VNImageRequestHandler(cgImage: image).perform([request])
    var words: [Word] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        for range in candidate.string.rangesOfWords() {
            let tokenBox = try candidate.boundingBox(for: range)?.boundingBox
            let wholeWord = range.lowerBound == candidate.string.startIndex
                && range.upperBound == candidate.string.endIndex
            guard let box = tokenBox ?? (wholeWord ? observation.boundingBox : nil) else { continue }
            words.append(Word(text: String(candidate.string[range]),
                              x: box.minX, y: 1 - box.maxY,
                              width: box.width, height: box.height,
                              confidence: Double(candidate.confidence)))
        }
    }
    let data = try JSONEncoder().encode(words)
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
    exit(1)
}

extension String {
    func rangesOfWords() -> [Range<String.Index>] {
        var ranges: [Range<String.Index>] = []
        var start: String.Index?
        for index in indices {
            if self[index].isWhitespace {
                if let first = start { ranges.append(first..<index) }
                start = nil
            } else if start == nil {
                start = index
            }
        }
        if let first = start { ranges.append(first..<endIndex) }
        return ranges
    }
}
