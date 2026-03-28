/*
 * Rule engine placeholder for extension-side scoring and button heuristics.
 * Extend this file with additional site-specific or generic detection rules.
 */

function evaluateDownloadCandidate(candidate) {
  if (!candidate || typeof candidate !== "object") {
    return { score: 0, accepted: false, reason: "invalid-candidate" };
  }

  const hasUrl = typeof candidate.url === "string" && candidate.url.length > 0;
  const hasFileSignal = !!candidate.filename || !!candidate.mimeType;
  const score = (hasUrl ? 60 : 0) + (hasFileSignal ? 40 : 0);

  return {
    score,
    accepted: score >= 60,
    reason: score >= 60 ? "likely-download" : "insufficient-signal",
  };
}

self.evaluateDownloadCandidate = evaluateDownloadCandidate;