import os
import pptx
from pptx.util import Inches, Pt
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor

def create_veritas_role_presentation(output_path="Veritas_Presentation.pptx"):
    prs = pptx.Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    # Harmonious Executive Dark Palette
    BG_COLOR = RGBColor(15, 23, 42)          # Deep Navy/Slate #0F172A
    CARD_BG = RGBColor(30, 41, 59)           # Card Background #1E293B
    CARD_BORDER = RGBColor(51, 65, 85)       # Border #334155
    CYAN = RGBColor(56, 189, 248)            # Accent Cyan #38BDF8 (Frontend / Primary)
    EMERALD = RGBColor(52, 211, 153)         # Verified Emerald #34D399 (Backend / Systems)
    PURPLE = RGBColor(192, 132, 252)         # AI Violet #C084FC (Data & ML)
    AMBER = RGBColor(251, 191, 36)           # Warning Amber #FBBF24
    ROSE = RGBColor(248, 113, 113)           # Danger Rose #F87171
    TEXT_WHITE = RGBColor(248, 250, 252)     # Crisp White #F8FAFC
    TEXT_MUTED = RGBColor(148, 163, 184)     # Silver #94A3B8
    TEXT_DIM = RGBColor(100, 116, 139)       # Slate Gray #64748B

    TOTAL_SLIDES = 12

    def set_slide_background(slide):
        bg = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(7.5)
        )
        bg.fill.solid()
        bg.fill.fore_color.rgb = BG_COLOR
        bg.line.fill.background()
        return bg

    def add_header(slide, title, category="VERITAS", role_badge=None, role_color=CYAN, slide_num=None):
        # Category Tag & Role Badge
        tag_box = slide.shapes.add_textbox(Inches(0.8), Inches(0.35), Inches(10.5), Inches(0.35))
        tf_tag = tag_box.text_frame
        tf_tag.word_wrap = True
        p_tag = tf_tag.paragraphs[0]
        
        run_cat = p_tag.add_run()
        run_cat.text = category.upper() + ("  •  " if role_badge else "")
        run_cat.font.size = Pt(10)
        run_cat.font.bold = True
        run_cat.font.color.rgb = TEXT_DIM

        if role_badge:
            run_badge = p_tag.add_run()
            run_badge.text = f"[{role_badge.upper()}]"
            run_badge.font.size = Pt(10.5)
            run_badge.font.bold = True
            run_badge.font.color.rgb = role_color

        # Title
        title_box = slide.shapes.add_textbox(Inches(0.8), Inches(0.65), Inches(10.5), Inches(0.8))
        tf_title = title_box.text_frame
        tf_title.word_wrap = True
        p_title = tf_title.paragraphs[0]
        p_title.text = title
        p_title.font.size = Pt(23)
        p_title.font.bold = True
        p_title.font.color.rgb = TEXT_WHITE

        # Divider line
        line = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(1.45), Inches(11.733), Inches(0.02)
        )
        line.fill.solid()
        line.fill.fore_color.rgb = CARD_BORDER
        line.line.fill.background()

        # Slide Number
        if slide_num:
            num_box = slide.shapes.add_textbox(Inches(11.3), Inches(0.4), Inches(1.2), Inches(0.4))
            p_num = num_box.text_frame.paragraphs[0]
            p_num.text = f"{slide_num:02d} / {TOTAL_SLIDES:02d}"
            p_num.alignment = PP_ALIGN.RIGHT
            p_num.font.size = Pt(10.5)
            p_num.font.bold = True
            p_num.font.color.rgb = TEXT_DIM

    def add_card(slide, left, top, width, height, title="", items=None, accent=CYAN, bg=CARD_BG):
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
        )
        card.fill.solid()
        card.fill.fore_color.rgb = bg
        card.line.color.rgb = CARD_BORDER
        card.line.width = Pt(1)

        if accent:
            stripe = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, Inches(left + 0.15), Inches(top + 0.02), Inches(width - 0.3), Inches(0.04)
            )
            stripe.fill.solid()
            stripe.fill.fore_color.rgb = accent
            stripe.line.fill.background()

        tb = slide.shapes.add_textbox(
            Inches(left + 0.25), Inches(top + 0.15), Inches(width - 0.5), Inches(height - 0.3)
        )
        tf = tb.text_frame
        tf.word_wrap = True

        if title:
            p_title = tf.paragraphs[0]
            p_title.text = title
            p_title.font.size = Pt(14.5)
            p_title.font.bold = True
            p_title.font.color.rgb = accent or TEXT_WHITE
            p_title.space_after = Pt(7)

        if items:
            for idx, item in enumerate(items):
                p = tf.add_paragraph() if (title or idx > 0) else tf.paragraphs[0]
                if isinstance(item, tuple):
                    bold_prefix, regular_text = item
                    run1 = p.add_run()
                    run1.text = bold_prefix + " "
                    run1.font.bold = True
                    run1.font.size = Pt(10.5)
                    run1.font.color.rgb = TEXT_WHITE

                    run2 = p.add_run()
                    run2.text = regular_text
                    run2.font.bold = False
                    run2.font.size = Pt(10.5)
                    run2.font.color.rgb = TEXT_MUTED
                else:
                    run = p.add_run()
                    run.text = item
                    run.font.size = Pt(10.5)
                    run.font.color.rgb = TEXT_MUTED
                p.space_after = Pt(5)
        return card

    def add_stat_box(slide, left, top, width, height, number, label, sublabel="", accent=CYAN):
        box = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
        )
        box.fill.solid()
        box.fill.fore_color.rgb = CARD_BG
        box.line.color.rgb = CARD_BORDER
        box.line.width = Pt(1)

        tb = slide.shapes.add_textbox(
            Inches(left + 0.1), Inches(top + 0.1), Inches(width - 0.2), Inches(height - 0.2)
        )
        tf = tb.text_frame
        tf.word_wrap = True

        p1 = tf.paragraphs[0]
        p1.text = number
        p1.alignment = PP_ALIGN.CENTER
        p1.font.size = Pt(26)
        p1.font.bold = True
        p1.font.color.rgb = accent

        p2 = tf.add_paragraph()
        p2.text = label
        p2.alignment = PP_ALIGN.CENTER
        p2.font.size = Pt(10.5)
        p2.font.bold = True
        p2.font.color.rgb = TEXT_WHITE
        p2.space_before = Pt(2)

        if sublabel:
            p3 = tf.add_paragraph()
            p3.text = sublabel
            p3.alignment = PP_ALIGN.CENTER
            p3.font.size = Pt(9)
            p3.font.color.rgb = TEXT_MUTED

    # =========================================================================
    # SLIDE 1: TITLE SLIDE
    # =========================================================================
    s1 = prs.slides.add_slide(blank_layout)
    set_slide_background(s1)

    glow = s1.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(1.8), Inches(0.12), Inches(3.6))
    glow.fill.solid()
    glow.fill.fore_color.rgb = CYAN
    glow.line.fill.background()

    t_box = s1.shapes.add_textbox(Inches(1.2), Inches(1.7), Inches(11.2), Inches(3.8))
    tf1 = t_box.text_frame
    tf1.word_wrap = True

    p0 = tf1.paragraphs[0]
    p0.text = "VERITAS SYSTEM ARCHITECTURE"
    p0.font.size = Pt(12)
    p0.font.bold = True
    p0.font.color.rgb = CYAN
    p0.space_after = Pt(8)

    p1 = tf1.add_paragraph()
    p1.text = "Explainable Fake & AI News Detection"
    p1.font.size = Pt(38)
    p1.font.bold = True
    p1.font.color.rgb = TEXT_WHITE
    p1.space_after = Pt(12)

    p2 = tf1.add_paragraph()
    p2.text = "A Multi-Modal Defense System Presented Across 3 Core Disciplines:"
    p2.font.size = Pt(16)
    p2.font.bold = True
    p2.font.color.rgb = TEXT_WHITE
    p2.space_after = Pt(8)

    p3 = tf1.add_paragraph()
    p3.text = "1. Frontend & Client UX   •   2. Backend Systems & Retrieval   •   3. Data Training & ML Forensics"
    p3.font.size = Pt(14)
    p3.font.color.rgb = AMBER
    p3.space_after = Pt(14)

    p4 = tf1.add_paragraph()
    p4.text = "In-Place Chrome Extension (MV3)  |  FastAPI & RSS Corroboration  |  Bilingual WhatsApp Bot  |  Gated Text ML"
    p4.font.size = Pt(12)
    p4.font.color.rgb = TEXT_MUTED

    add_card(
        s1, left=0.8, top=5.6, width=11.733, height=1.3,
        title="",
        items=[
            ("Group Presentation Strategy:", "Each team member presents their dedicated engineering discipline, demonstrating complete end-to-end integration."),
            ("Core Thesis:", "Explainable, deterministic mathematics and multi-source corroboration replace opaque black-box AI scores.")
        ],
        accent=EMERALD
    )

    # =========================================================================
    # SLIDE 2: THE 3-ROLE RESPONSIBILITY MATRIX
    # =========================================================================
    s2 = prs.slides.add_slide(blank_layout)
    set_slide_background(s2)
    add_header(s2, "Team Roles & Engineering Ownership Matrix", "Group Organization", None, CYAN, 2)

    add_card(
        s2, left=0.8, top=1.75, width=3.65, height=5.0,
        title="Role 1: Frontend & UI Lead",
        items=[
            ("Primary Focus:", "User interaction, client platforms & visual explainability."),
            ("Code Modules:", "extension/content.js, extension/background.js, bot/whatsapp_bot.py, webapp/privacy.html."),
            ("Core Innovations:", "Shadow DOM banner (0% CSS collision), Manifest V3 service worker relay, bilingual WhatsApp delivery."),
            ("Slides Owned:", "Slide 10 (Chrome Extension), Slide 11 (WhatsApp Bot)."),
            ("Live Demo:", "In-browser live news reading & WhatsApp claim forwarding.")
        ],
        accent=CYAN
    )

    add_card(
        s2, left=4.85, top=1.75, width=3.65, height=5.0,
        title="Role 2: Backend Systems Lead",
        items=[
            ("Primary Focus:", "FastAPI architecture, parallel news retrieval & verification math."),
            ("Code Modules:", "backend/app/main.py, pipeline.py, cross_check.py, verification.py, security.py, tests/."),
            ("Core Innovations:", "Parallel RSS retrieval across Tier 1/2/3 outlets, Anti-SSRF private IP firewall, 262-test suite."),
            ("Slides Owned:", "Slide 3 (Problem), Slide 4 (Architecture), Slide 8 (Evidence Math), Slide 9 (Testing/Security)."),
            ("Live Demo:", "Sub-second FastAPI pipeline & automated test execution.")
        ],
        accent=EMERALD
    )

    add_card(
        s2, left=8.9, top=1.75, width=3.65, height=5.0,
        title="Role 3: Data & ML Models Lead",
        items=[
            ("Primary Focus:", "Training pipelines, feature engineering, validation gating & AI forensics."),
            ("Code Modules:", "backend/tools/train_*.py, indic_dataset.py, text_model.py, ai_signals.py, text_analysis.py."),
            ("Core Innovations:", "Multinomial Naive Bayes without metadata leakage, 70% validation precision gate, C2PA & SynthID."),
            ("Slides Owned:", "Slide 5 (Training & Datasets), Slide 6 (AI Forensics), Slide 7 (Validation Gating), Slide 12 (Roadmap)."),
            ("Live Demo:", "Model artifact inspection & provenance marker decoding.")
        ],
        accent=PURPLE
    )

    # =========================================================================
    # SLIDE 3: ROLE 2 (BACKEND) - THE CRISIS & MOTIVATION
    # =========================================================================
    s3 = prs.slides.add_slide(blank_layout)
    set_slide_background(s3)
    add_header(s3, "The Crisis of Unchecked Synthetic Media & Disinformation", "Domain Problem", "Role 2: Backend Systems", EMERALD, 3)

    add_card(
        s3, left=0.8, top=1.75, width=3.65, height=5.0,
        title="1. Generative AI Explosion",
        items=[
            ("Zero-Cost Misinformation:", "Modern LLMs generate convincing clickbait articles and fabricated news at machine scale."),
            ("Synthetic Imagery:", "AI-generated images (Midjourney, Stable Diffusion) accompany false claims without disclosure."),
            ("Algorithmic Velocity:", "Fake stories outpace human fact-checking organizations within minutes of publication.")
        ],
        accent=ROSE
    )

    add_card(
        s3, left=4.85, top=1.75, width=3.65, height=5.0,
        title="2. The 'Black Box' Trap",
        items=[
            ("Arbitrary Outputs:", "Existing AI tools output opaque numbers like '82% Fake' without citing primary sources."),
            ("LLM Hallucinations:", "Pure-LLM fact-checkers often hallucinate false corroboration or contradict historical facts."),
            ("Zero User Trust:", "Without verifiable links, readers dismiss automated warnings as biased censorship.")
        ],
        accent=AMBER
    )

    add_card(
        s3, left=8.9, top=1.75, width=3.65, height=5.0,
        title="3. The Veritas Answer",
        items=[
            ("Multi-Signal Fusion:", "Combines media provenance, stylometrics, machine learning, and live news RSS corroboration."),
            ("Deterministic Guarantees:", "Verdicts derived from verifiable mathematical evidence formulas, not LLM gut feelings."),
            ("In-Place Intervention:", "Protects readers directly inside browser and chat interfaces before lies spread.")
        ],
        accent=EMERALD
    )

    # =========================================================================
    # SLIDE 4: ROLE 2 (BACKEND) - SYSTEM ARCHITECTURE
    # =========================================================================
    s4 = prs.slides.add_slide(blank_layout)
    set_slide_background(s4)
    add_header(s4, "End-to-End System Architecture & Data Flow", "System Engineering", "Role 2: Backend Systems", EMERALD, 4)

    add_card(
        s4, left=0.8, top=1.75, width=3.65, height=5.0,
        title="Client & Ingestion Tier",
        items=[
            ("Chrome Extension (MV3):", "Extracts headline & body via content script; zero style-clash Shadow DOM banner."),
            ("Service Worker Relay:", "Background worker proxies API calls to localhost:8000, avoiding CORS/mixed-content."),
            ("WhatsApp Twilio Webhook:", "Dedicated webhook on port 8001 parses forwarded news URLs and messages."),
            ("SSRF Security Gateway:", "Sanitizes URLs, validates schemes, and blocks private/loopback IP address ranges.")
        ],
        accent=CYAN
    )

    add_card(
        s4, left=4.85, top=1.75, width=3.65, height=5.0,
        title="FastAPI Orchestration Core",
        items=[
            ("Pipeline Dispatcher:", "Fires 4 detection signals asynchronously in sub-second execution time."),
            ("Claim Extraction Engine:", "Structured claim distillation (Gemini with zero-key regex fallback)."),
            ("Parallel RSS Corroborator:", "Dispatches 2–3 targeted queries to Google News RSS across 5–10 reference outlets."),
            ("Source Registry:", "Weighted Tier 1 (Reuters, BBC, PTI), Tier 2 (Mainstream), and Tier 3 media outlets.")
        ],
        accent=EMERALD
    )

    add_card(
        s4, left=8.9, top=1.75, width=3.65, height=5.0,
        title="Evidence & Decision Engine",
        items=[
            ("Evidence Math Engine:", "Computes bounded score E = 0.30M + 0.30C + 0.20F + 0.20S."),
            ("Safety Guardrails:", "Contradiction penalty (-15) and breaking news median freshness gate."),
            ("Audit Logging:", "Emits structured diagnostics with full explanation for every flagged item."),
            ("Verdict Categorization:", "Maps score to: TRUSTED, UNCERTAIN, SUSPICIOUS, or FAKE.")
        ],
        accent=PURPLE
    )

    # =========================================================================
    # SLIDE 5: ROLE 3 (DATA & ML) - DATASETS & TRAINING PIPELINE
    # =========================================================================
    s5 = prs.slides.add_slide(blank_layout)
    set_slide_background(s5)
    add_header(s5, "Data Training Pipeline, Datasets & Feature Engineering", "Machine Learning", "Role 3: Data & ML", PURPLE, 5)

    add_card(
        s5, left=0.8, top=1.75, width=3.65, height=5.0,
        title="Dataset Curation & Splits",
        items=[
            ("LIAR Benchmark:", "Trained on fact-checked claims (Wang, ACL 2017) mapped to 3 classes: false, mixed, supported."),
            ("Indic News Corpora:", "Multi-outlet Indian news dataset spanning English and Hindi regional publications."),
            ("Zero Metadata Leakage:", "Training is restricted strictly to statement text; excludes speaker, party, and venue to prevent unfair bias."),
            ("Strict Split Separation:", "Exact and near-duplicate claims grouped together to guarantee zero leakage across train/val/test.")
        ],
        accent=PURPLE
    )

    add_card(
        s5, left=4.85, top=1.75, width=3.65, height=5.0,
        title="Feature Engineering & NLP",
        items=[
            ("Unicode Indic Tokenizer:", "Custom script-aware tokenizer preserving Devanagari and regional linguistic tokens."),
            ("Brand Token Redaction:", "Automatically strips publisher brand names and outlet terms so models cannot memorize reputations."),
            ("Word & Bigram Extraction:", "Captures nuanced contextual phrases without explosion in parameter space."),
            ("Text-Length Guard:", "Rejects short (<30 tokens) or out-of-distribution inputs rather than relying on ungrounded priors.")
        ],
        accent=CYAN
    )

    add_card(
        s5, left=8.9, top=1.75, width=3.65, height=5.0,
        title="Multinomial Naive Bayes Model",
        items=[
            ("Pure JSON Artifacts:", "Stored in validated JSON (liar_mnb_v1.json); zero pickle risk, zero heavy ML framework overhead."),
            ("Reliability Calibration:", "Binned isotonic reliability curves map raw posterior probabilities into calibrated risk scores."),
            ("Deterministic Re-training:", "Reproducible via `python tools/train_text_model.py` with embedded SHA-256 dataset checks."),
            ("Complete Model Card:", "Full provenance documented in MODEL_CARD.md covering limitations and intended scope.")
        ],
        accent=EMERALD
    )

    # =========================================================================
    # SLIDE 6: ROLE 3 (DATA & ML) - AI FORENSICS & STYLOMETRICS
    # =========================================================================
    s6 = prs.slides.add_slide(blank_layout)
    set_slide_background(s6)
    add_header(s6, "Multi-Modal AI Forensics & Stylometric Signals", "AI Detection", "Role 3: Data & ML", PURPLE, 6)

    add_card(
        s6, left=0.8, top=1.75, width=3.65, height=5.0,
        title="1. Media Provenance Forensics",
        items=[
            ("C2PA Content Credentials:", "Inspects cryptographically signed JUMBF manifests verifying image provenance."),
            ("IPTC Digital Source Type:", "Extracts digital source tags (compositeSynthetic, trainedAlgorithmicMedia)."),
            ("SynthID Byte Markers:", "Detects DeepMind SynthID imperceptible watermarks embedded in synthetic imagery."),
            ("Generator EXIF Headers:", "Flags signature tags from Midjourney, Stable Diffusion, and Adobe Firefly.")
        ],
        accent=CYAN
    )

    add_card(
        s6, left=4.85, top=1.75, width=3.65, height=5.0,
        title="2. Stylometric Writing Analysis",
        items=[
            ("Sentence-Length Burstiness:", "Calculates standard deviation of sentence lengths (LLMs exhibit unnaturally flat variance)."),
            ("Lexical Richness (TTR):", "Type-Token Ratio reveals robotic repetitive vocabulary typical of synthetic generation."),
            ("Stock LLM Expressions:", "High-precision dictionary flagging synthetic clichés ('delve', 'tapestry of', 'it is crucial to note')."),
            ("Clickbait & Punctuation:", "Monitors excessive capitalization, sensationalist adjectives, and exclamation density.")
        ],
        accent=PURPLE
    )

    add_card(
        s6, left=8.9, top=1.75, width=3.65, height=5.0,
        title="3. Multi-Signal Fusion Math",
        items=[
            ("Signed Signal Weights:", "Each signal emits an auditable diagnostic score and confidence rating."),
            ("Explainable Breakdown:", "The backend returns an itemized evidence log explaining the origin of every flagged trait."),
            ("Auditable Transparency:", "Users and researchers can inspect the exact provenance tags and stylistic markers."),
            ("No Single-Point Failure:", "Media provenance corroborates text analysis; one alone never dictates false verdicts.")
        ],
        accent=AMBER
    )

    # =========================================================================
    # SLIDE 7: ROLE 3 (DATA & ML) - VALIDATION GATING & ETHICS
    # =========================================================================
    s7 = prs.slides.add_slide(blank_layout)
    set_slide_background(s7)
    add_header(s7, "Validation Gating & Ethical Machine Learning", "Model Governance", "Role 3: Data & ML", PURPLE, 7)

    add_card(
        s7, left=0.8, top=1.75, width=5.65, height=2.45,
        title="1. Strict 70% Validation Precision Gate",
        items=[
            ("The Ethical Rule:", "A machine learning text classifier is NOT permitted to influence suspicion unless its holdout validation precision clears >= 70%."),
            ("The LIAR Baseline Result:", "At false >= 0.80 operating point, validation precision is 45.29%."),
            ("The Enforcement:", "The artifact is deployed in 'shadow-only mode' (0.00 weight) — returned for developer diagnostics, but locked out of verdicts.")
        ],
        accent=ROSE
    )

    add_card(
        s7, left=6.85, top=1.75, width=5.68, height=2.45,
        title="2. Shadow Deployment Paradigm",
        items=[
            ("Safe Experimentation:", "New experimental models can be evaluated live against production traffic without risking false user alerts."),
            ("Audit Trail:", "The model prediction and calibration curves appear in `ai_signals['text_classifier']` for verification."),
            ("Fail-Safe Default:", "Missing or corrupt deployment metadata causes runtime rejection; never silent elevation.")
        ],
        accent=AMBER
    )

    add_card(
        s7, left=0.8, top=4.4, width=5.65, height=2.45,
        title="3. Preventing Algorithmic Overfitting",
        items=[
            ("Temporal Partitioning:", "Evaluates models chronologically (older news for training, strictly newer news for test)."),
            ("Outlet-Based Partitioning:", "Holds out entire news outlets to verify that the classifier generalizes across unfamiliar publishers."),
            ("Publisher Redaction:", "Guarantees the model cannot learn spurious correlations between publication names and labels.")
        ],
        accent=PURPLE
    )

    add_card(
        s7, left=6.85, top=4.4, width=5.68, height=2.45,
        title="4. Ethical Machine Learning Guarantees",
        items=[
            ("Refusal on Low Coverage:", "If test text shares < 10% vocabulary with training data, the model returns INSUFFICIENT_VOCABULARY."),
            ("Conservative Thresholding:", "Avoids aggressive automated censorship; prioritizes precision over raw recall."),
            ("Human-in-the-Loop Transparency:", "Users see the exact words and signals rather than taking the model's word blindly.")
        ],
        accent=EMERALD
    )

    # =========================================================================
    # SLIDE 8: ROLE 2 (BACKEND) - EVIDENCE SCORING MATHEMATICS
    # =========================================================================
    s8 = prs.slides.add_slide(blank_layout)
    set_slide_background(s8)
    add_header(s8, "Evidence Score Mathematical Formulation", "Formal Math Engine", "Role 2: Backend Systems", EMERALD, 8)

    add_card(
        s8, left=0.8, top=1.75, width=11.733, height=1.3,
        title="Core Evidence Formulation (Bounded 0 – 100)",
        items=[
            ("Formula:", "E = 0.30 · M  +  0.30 · C  +  0.20 · F  +  0.20 · S"),
            ("Deterministic Guarantee:", "Replaces neural network 'black boxes' with a verifiable mathematical score where higher E indicates substantiated truth.")
        ],
        accent=CYAN
    )

    add_card(
        s8, left=0.8, top=3.25, width=2.75, height=3.5,
        title="M : Semantic Similarity",
        items=[
            ("Formula:", "0.25·Jac + 0.45·Rec + 0.20·Ratio + 0.10·Entity"),
            ("Recall Focus:", "Measures coverage of key content words; handles headline paraphrasing."),
            ("Entity Bonus:", "Rewards shared proper nouns across distinct newsrooms.")
        ],
        accent=CYAN
    )

    add_card(
        s8, left=3.8, top=3.25, width=2.75, height=3.5,
        title="C : Source Consensus",
        items=[
            ("Formula:", "C = ((A + 0.5·U) / (A + U + D)) × 100"),
            ("A (Agreed):", "Tier-weighted supporting outlets."),
            ("D (Disagreed):", "Tier-weighted contradicting outlets."),
            ("U (Unknown):", "Outlets covering story without stance.")
        ],
        accent=EMERALD
    )

    add_card(
        s8, left=6.8, top=3.25, width=2.75, height=3.5,
        title="F : Factual Consistency",
        items=[
            ("Key Checks:", "Dates, figures, casualty metrics, and geolocations cross-matching."),
            ("Google Fact Check API:", "Blends verified third-party fact-checker reviews (PolitiFact, Snopes, Vishvas News).")
        ],
        accent=PURPLE
    )

    add_card(
        s8, left=9.8, top=3.25, width=2.733, height=3.5,
        title="S : Source Quality",
        items=[
            ("Tier 1 (1.00):", "Global wire agencies (Reuters, AP, BBC, The Hindu)."),
            ("Tier 2 (0.85):", "National mainstream dailies."),
            ("Tier 3 (0.65):", "Regional news portals."),
            ("Unknown (0.40):", "Unverified blogs & portals.")
        ],
        accent=AMBER
    )

    # =========================================================================
    # SLIDE 9: ROLE 2 (BACKEND) - PRODUCTION ENGINEERING & TESTING
    # =========================================================================
    s9 = prs.slides.add_slide(blank_layout)
    set_slide_background(s9)
    add_header(s9, "Production Engineering, Security & Test Coverage", "Backend Robustness", "Role 2: Backend Systems", EMERALD, 9)

    add_stat_box(s9, left=0.8, top=1.75, width=2.7, height=1.45, number="262", label="Automated Tests", sublabel="100% Pass Rate in Pytest", accent=CYAN)
    add_stat_box(s9, left=3.8, top=1.75, width=2.7, height=1.45, number="0 MB", label="Heavy ML Bloat", sublabel="Stdlib + FastAPI Only", accent=EMERALD)
    add_stat_box(s9, left=6.8, top=1.75, width=2.7, height=1.45, number="100%", label="Anti-SSRF Shield", sublabel="Private IP & DNS Filters", accent=AMBER)
    add_stat_box(s9, left=9.8, top=1.75, width=2.733, height=1.45, number="1 Hour", label="Chaos Soak Harness", sublabel="9 System Invariants Tested", accent=PURPLE)

    add_card(
        s9, left=0.8, top=3.4, width=5.6, height=3.35,
        title="Lightweight & High-Throughput Engine",
        items=[
            ("Zero Heavy Dependencies:", "Runs entirely on Python standard library and FastAPI; sub-second cold starts on standard CPUs."),
            ("Parallel Non-Blocking RSS:", "Asynchronously queries multiple RSS search feeds concurrently; prevents latency bottlenecks."),
            ("Contradiction Invariant (-15):", "If any Tier-1 outlet refutes a claim, a flat -15 deduction is immediately imposed on evidence score E."),
            ("Breaking News Freshness Gate:", "Checks median reference age (<= 2.0h); protects fast-breaking events from false accusations.")
        ],
        accent=EMERALD
    )

    add_card(
        s9, left=6.8, top=3.4, width=5.733, height=3.35,
        title="Security Firewall & Privacy Architecture",
        items=[
            ("Anti-SSRF Protection:", "Rejects private IP ranges (127.0.0.1, 10.0.0.0/8, 192.168.0.0/16) and non-HTTP schemes."),
            ("Token-Bucket Rate Limiter:", "Prevents volumetric denial-of-service abuse and scraping floods on API endpoints."),
            ("Zero Telemetry Retention:", "Articles analyzed on-the-fly; no user reading history or PII is ever stored in databases."),
            ("Deterministic Soak Testing:", "1-hour chaos soak test asserts 9 structural invariants across thousands of simulated edge-cases.")
        ],
        accent=CYAN
    )

    # =========================================================================
    # SLIDE 10: ROLE 1 (FRONTEND) - CHROME EXTENSION (MV3)
    # =========================================================================
    s10 = prs.slides.add_slide(blank_layout)
    set_slide_background(s10)
    add_header(s10, "In-Place Chrome Extension (Manifest V3 & Shadow DOM)", "Frontend Engineering", "Role 1: Frontend & UI", CYAN, 10)

    add_card(
        s10, left=0.8, top=1.75, width=5.6, height=5.0,
        title="Frictionless In-Place Experience",
        items=[
            ("Zero Copy-Pasting Required:", "Automatically inspects news articles as users browse; activates only when news content is detected."),
            ("Shadow DOM Root Encapsulation:", "All banner HTML/CSS is isolated inside a closed Shadow DOM, guaranteeing 0% style collision with host sites."),
            ("Service Worker API Relay:", "Chrome MV3 background service worker proxies analysis requests, preventing HTTPS mixed-content blocks."),
            ("Color-Coded Verdict Hierarchy:", "Clear visual state: Green (Trusted), Blue/Yellow (Uncertain), Orange (Suspicious), Red (Fake)."),
            ("Interactive Evidence Accordion:", "Users can click 'Inspect Evidence' to drill into supporting vs. contradicting sources and AI stylometric flags.")
        ],
        accent=CYAN
    )

    banner_img_path = os.path.abspath(os.path.join("assets", "banner-live.png"))
    if not os.path.exists(banner_img_path):
        banner_img_path = os.path.abspath("banner-live.png")
    if os.path.exists(banner_img_path):
        card_img = s10.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(6.8), Inches(1.75), Inches(5.733), Inches(5.0)
        )
        card_img.fill.solid()
        card_img.fill.fore_color.rgb = CARD_BG
        card_img.line.color.rgb = CARD_BORDER

        s10.shapes.add_picture(banner_img_path, Inches(7.1), Inches(2.15), width=Inches(5.15))

        tb_caption = s10.shapes.add_textbox(Inches(7.1), Inches(5.6), Inches(5.15), Inches(0.95))
        tf_cap = tb_caption.text_frame
        tf_cap.word_wrap = True
        p_c1 = tf_cap.paragraphs[0]
        p_c1.text = "Live In-Browser Shadow DOM Preview"
        p_c1.font.size = Pt(12)
        p_c1.font.bold = True
        p_c1.font.color.rgb = TEXT_WHITE

        p_c2 = tf_cap.add_paragraph()
        p_c2.text = "Real-time non-intrusive alert banner showing suspicion rating, verified news links, and fact-checking rationale."
        p_c2.font.size = Pt(10)
        p_c2.font.color.rgb = TEXT_MUTED
    else:
        add_card(
            s10, left=6.8, top=1.75, width=5.733, height=5.0,
            title="Interactive Shadow DOM Banner",
            items=[
                ("Live Banner:", "Renders at the top of analyzed news pages."),
                ("Expandable Accordion:", "Full inspection of individual source links and claims.")
            ],
            accent=EMERALD
        )

    # =========================================================================
    # SLIDE 11: ROLE 1 (FRONTEND) - WHATSAPP BOT INTEGRATION
    # =========================================================================
    s11 = prs.slides.add_slide(blank_layout)
    set_slide_background(s11)
    add_header(s11, "WhatsApp Fact-Checking Assistant (Chat Interface)", "Mobile Accessibility", "Role 1: Frontend & UI", CYAN, 11)

    add_card(
        s11, left=0.8, top=1.75, width=5.6, height=5.0,
        title="Direct Mobile Verification",
        items=[
            ("Twilio Webhook Service:", "Lightweight FastAPI webhook server running on Port 8001 responding to incoming WhatsApp messages."),
            ("Zero-Friction Ingestion:", "Users forward any suspicious article URL or copied claim directly to the Veritas WhatsApp contact."),
            ("Bilingual English & Hindi Responses:", "Automatically formats verdicts in both English and Hindi for maximum accessibility across regional user bases."),
            ("Fighting Dark Social Misinformation:", "Brings real-time fact-checking directly to private chat networks where viral hoaxes circulate unmoderated."),
            ("1-Click Launchers:", "Equipped with START_WHATSAPP_BOT.bat and TEST_BOT.bat for rapid testing and demonstrations.")
        ],
        accent=EMERALD
    )

    add_card(
        s11, left=6.8, top=1.75, width=5.733, height=5.0,
        title="Live WhatsApp Conversation Flow",
        items=[
            ("User Input Message:", "Forwarded message: 'UNESCO declares Indian National Anthem the best in the world!'"),
            ("Bot Output (English):", "🛡️ Veritas Verification\n• Verdict: FAKE NEWS (Suspicion: 92/100)\n• Evidence: Contradicted by UNESCO & PTI\n• Fact Check: Debunked by Snopes & Vishvas News\n• Read Trusted Report: https://thehindu.com/..."),
            ("Bot Output (Hindi):", "🛡️ वेरिटास सत्यापन\n• निष्कर्ष: फर्जी खबर (स्कोर: 92/100)\n• स्रोत: यूनेस्को और पीटीआई द्वारा खंडन किया गया।"),
            ("Turnaround Speed:", "Sub-2.5 second total round-trip response time.")
        ],
        accent=CYAN
    )

    # =========================================================================
    # SLIDE 12: ALL ROLES - SUMMARY, ROADMAP & Q&A
    # =========================================================================
    s12 = prs.slides.add_slide(blank_layout)
    set_slide_background(s12)
    add_header(s12, "Project Summary, Future Roadmap & Team Q&A", "Conclusion & Handoff", "All Team Members", AMBER, 12)

    add_card(
        s12, left=0.8, top=1.75, width=3.65, height=5.0,
        title="Frontend & UI Summary",
        items=[
            ("Key Deliverable:", "Chrome MV3 Extension with Shadow DOM & Bilingual WhatsApp Bot."),
            ("User Impact:", "Brings in-place verification directly to users across web and chat channels."),
            ("Future Roadmap:", "Firefox & Safari extension ports; mobile share-sheet integrations.")
        ],
        accent=CYAN
    )

    add_card(
        s12, left=4.85, top=1.75, width=3.65, height=5.0,
        title="Backend Systems Summary",
        items=[
            ("Key Deliverable:", "FastAPI engine with parallel RSS cross-checking & Evidence Math (E)."),
            ("Engineering Impact:", "Sub-second execution, zero heavy ML bloat, anti-SSRF firewall, 262 passing unit tests."),
            ("Future Roadmap:", "Distributed Redis caching & horizontal multi-region RSS worker nodes.")
        ],
        accent=EMERALD
    )

    add_card(
        s12, left=8.9, top=1.75, width=3.65, height=5.0,
        title="Data & ML Summary",
        items=[
            ("Key Deliverable:", "Multinomial Naive Bayes model with 70% precision gate & C2PA provenance."),
            ("Algorithmic Impact:", "Guarantees that unverified statistical models never falsely accuse news stories."),
            ("Future Roadmap:", "On-device WebAssembly SLM reasoning & synthetic video deepfake detection.")
        ],
        accent=PURPLE
    )

    prs.save(output_path)
    print(f"Role-based presentation saved successfully to {output_path}")

if __name__ == "__main__":
    create_veritas_role_presentation()
