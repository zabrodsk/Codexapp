# Acne Risk Scanner App Plan

## Goal
Deliver a camera-first iOS app that scans barcodes or ingredient photos, evaluates acne/irritation risk, and presents an explainable score with tips.

## Core User Flow
1. Scan barcode or capture product/ingredients photo.
2. Identify product or extract ingredient list.
3. Normalize ingredients and score acne/irritation risk.
4. Present score, top drivers, and skin-type notes.
5. Offer tips and safer alternatives.

## Technical Approaches
### Option A: Barcode-First (MVP Primary)
- Scan barcode via AVFoundation/Vision.
- Look up product in cosmetics database.
- Pull ingredient list.
- Run acne scoring logic.

Pros: reliable product ID, fast, low hallucination risk.
Cons: fails when product missing from database.

Suggested data sources:
- Open Beauty Facts
- INCI Decoder
- CosDNA

### Option B: Photo + OCR + AI (Fallback)
- Capture ingredient photo.
- OCR via Apple Vision.
- Normalize INCI names.
- Score acne risk using ingredient weights.

Pros: works without barcode, “magic” UX.
Cons: OCR errors, needs guardrails.

## Scoring Logic (Core IP)
- Ingredient-level weights based on:
  - Comedogenic rating (0–5)
  - Irritation potential
  - Fungal acne trigger risk
  - Oil solubility
  - Ingredient order (proxy for concentration)
- Normalize to 0–100 final score.

Example ranges:
- 0–20: Acne-safe
- 21–40: Low risk
- 41–60: Medium risk
- 61–80: High risk
- 81–100: Avoid if acne-prone

## Output Design
- Show clear score with visual risk indicator.
- Explain “why” with top problematic ingredients.
- Call out affected skin types.
- Highlight ingredients inline for learnability.

## Differentiation Ideas
- Personalization by skin type.
- Acne-type specific scoring (hormonal, fungal, comedonal).
- Scan history + correlation with breakouts.

## MVP Roadmap
### Week 1
- Barcode scanning
- Open Beauty Facts integration
- Static acne scoring

### Week 2
- Ingredient-level explanations
- Clean UI
- Save scan history

### Week 3
- OCR fallback
- Ingredient normalization
- Personalization slider

## Business Notes
- Free daily scans + Pro tier.
- Affiliate links to acne-safe alternatives.
- Educational framing (not medical).
