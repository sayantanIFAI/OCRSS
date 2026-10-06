"""Curated seed terminology map (ARCHITECTURE §5 S5): ~120 concepts.

~35 conditions + ~10 symptoms + ~4 procedure families (SNOMED CT, conditions also carry ICD-10),
~30 lab analytes + ~14 vital signs (LOINC), ~15 drugs + ~6 allergens (SNOMED CT substances), plus
``ROUTE_SCT``. ``FREQ_PER_DAY`` and ``UCUM_ALIASES`` live in ``terminology.parse``.

PROVENANCE: the codes quoted in ARCHITECTURE §8.2 are used verbatim. The rest are curated from
general clinical-coding knowledge and have **not** been checked against a licensed SNOMED CT India
/ LOINC / ICD-10 release. Backlog E3-S1 loads those releases and replaces this map as the primary
source (this file becomes the offline fallback, E3-S5). Anything not in the map stays
``local_only`` with its text preserved — nothing is ever force-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ICD10 = "http://hl7.org/fhir/sid/icd-10"


@dataclass(frozen=True)
class Concept:
    system: str
    code: str
    display: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    icd10: str | None = None


def _c(code, display, *aliases, icd10=None, system=SNOMED) -> Concept:
    return Concept(system, code, display, tuple(aliases), icd10)


def _l(code, display, *aliases) -> Concept:
    return Concept(LOINC, code, display, tuple(aliases))


CONDITIONS = (
    _c("44054006", "Type 2 diabetes mellitus", "t2dm", "type 2 diabetes", "type ii diabetes",
       "dm type 2", "type 2 dm", "niddm", "dm2", icd10="E11"),
    _c("46635009", "Type 1 diabetes mellitus", "t1dm", "type 1 diabetes", "iddm", icd10="E10"),
    _c("73211009", "Diabetes mellitus", "diabetes", "dm", icd10="E14"),
    _c("59621000", "Essential hypertension", "hypertension", "htn", "high blood pressure",
       "systemic hypertension", icd10="I10"),
    _c("55822004", "Hyperlipidemia", "dyslipidemia", "dyslipidaemia", "hyperlipidaemia",
       icd10="E78.5"),
    _c("13644009", "Hypercholesterolemia", "high cholesterol", icd10="E78.0"),
    _c("53741008", "Coronary arteriosclerosis", "coronary artery disease", "cad",
       "single vessel coronary artery disease", "triple vessel disease", "ihd",
       icd10="I25.1"),
    _c("22298006", "Myocardial infarction", "mi", "heart attack", "stemi", "nstemi",
       icd10="I21.9"),
    _c("49436004", "Atrial fibrillation", "af", "afib", icd10="I48"),
    _c("84114007", "Heart failure", "chf", "congestive heart failure", "ccf", icd10="I50.9"),
    _c("230690007", "Cerebrovascular accident", "stroke", "cva", icd10="I64"),
    _c("195967001", "Asthma", "bronchial asthma", icd10="J45"),
    _c("13645005", "Chronic obstructive pulmonary disease", "copd", icd10="J44.9"),
    _c("233604007", "Pneumonia", icd10="J18.9"),
    _c("385093006", "Community acquired pneumonia", "cap", icd10="J18.9"),
    _c("54150009", "Upper respiratory infection", "urti", "uri",
       "upper respiratory tract infection", icd10="J06.9"),
    _c("56717001", "Tuberculosis", "tb", "pulmonary tuberculosis", "koch's", icd10="A16.9"),
    _c("68566005", "Urinary tract infection", "uti", icd10="N39.0"),
    _c("709044004", "Chronic kidney disease", "ckd", "chronic renal failure", icd10="N18.9"),
    _c("40930008", "Hypothyroidism", icd10="E03.9"),
    _c("34486009", "Hyperthyroidism", "thyrotoxicosis", icd10="E05.9"),
    _c("271737000", "Anemia", "anaemia", icd10="D64.9"),
    _c("414916001", "Obesity", icd10="E66.9"),
    _c("235595009", "Gastroesophageal reflux disease", "gerd", "gord", "acid reflux",
       icd10="K21.9"),
    _c("25374005", "Gastroenteritis", "age", "acute gastroenteritis", icd10="A09"),
    _c("197321007", "Steatosis of liver", "fatty liver", "nafld", icd10="K76.0"),
    _c("37796009", "Migraine", icd10="G43.9"),
    _c("84757009", "Epilepsy", "seizure disorder", icd10="G40.9"),
    _c("396275006", "Osteoarthritis", "oa", icd10="M19.9"),
    _c("69896004", "Rheumatoid arthritis", "ra", icd10="M06.9"),
    _c("64859006", "Osteoporosis", icd10="M81.9"),
    _c("35489007", "Depressive disorder", "depression", icd10="F32.9"),
    _c("197480006", "Anxiety disorder", "anxiety", icd10="F41.9"),
    _c("38362002", "Dengue", "dengue fever", icd10="A90"),
    _c("61462000", "Malaria", icd10="B54"),
    _c("4834000", "Typhoid fever", "typhoid", "enteric fever", icd10="A01.0"),
    _c("266569009", "Benign prostatic hyperplasia", "bph", icd10="N40"),
)  # fmt: skip

SYMPTOMS = (
    _c("386661006", "Fever", "pyrexia", "febrile"),
    _c("49727002", "Cough"),
    _c("25064002", "Headache"),
    _c("29857009", "Chest pain"),
    _c("267036007", "Dyspnea", "breathlessness", "shortness of breath", "sob", "dyspnoea"),
    _c("21522001", "Abdominal pain", "pain abdomen", "stomach ache"),
    _c("422587007", "Nausea"),
    _c("422400008", "Vomiting"),
    _c("84229001", "Fatigue", "tiredness", "weakness"),
    _c("404640003", "Dizziness", "giddiness", "vertigo"),
    _c("62315008", "Diarrhea", "loose motions", "diarrhoea"),
    _c("80313002", "Palpitations"),
)  # fmt: skip

PROCEDURES = (
    _c("415070008", "Percutaneous coronary intervention", "ptca", "pci", "angioplasty",
       "ptca to lad", "coronary angioplasty"),
    _c("33367005", "Coronary angiography", "coronary angiogram", "cag", "angiography"),
    _c("232717009", "Coronary artery bypass grafting", "cabg", "bypass surgery"),
    _c("40701008", "Echocardiography", "echo", "2d echo"),
    _c("399208008", "Plain chest X-ray", "chest x-ray", "cxr", "x-ray chest",
       "chest x-ray pa view"),
    _c("80146002", "Appendectomy", "appendicectomy"),
    _c("38102005", "Cholecystectomy", "lap cholecystectomy"),
)  # fmt: skip

LABS = (
    _l("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "hba1c", "glycated hemoglobin",
       "glycosylated haemoglobin", "a1c"),
    _l("1558-6", "Fasting glucose [Mass/volume] in Serum or Plasma", "fasting blood glucose",
       "fbs", "fbg", "fasting blood sugar", "fasting plasma glucose", "fpg"),
    _l("1521-4", "Glucose [Mass/volume] in Serum or Plasma --2 hours post meal", "ppbs",
       "post prandial blood sugar", "pp blood sugar", "2 hr pp glucose"),
    _l("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "glucose", "random blood sugar",
       "rbs", "blood glucose"),
    _l("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "creatinine",
       "serum creatinine", "s creatinine"),
    _l("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma", "bun",
       "blood urea nitrogen"),
    _l("3091-6", "Urea [Mass/volume] in Serum or Plasma", "urea", "blood urea",
       "serum urea"),
    _l("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "total cholesterol",
       "cholesterol", "serum cholesterol"),
    _l("2089-1", "Cholesterol in LDL [Mass/volume] in Serum or Plasma", "ldl",
       "ldl cholesterol", "ldl-c"),
    _l("2085-9", "Cholesterol in HDL [Mass/volume] in Serum or Plasma", "hdl",
       "hdl cholesterol", "hdl-c"),
    _l("2571-8", "Triglyceride [Mass/volume] in Serum or Plasma", "triglycerides", "tg",
       "triglyceride"),
    _l("718-7", "Hemoglobin [Mass/volume] in Blood", "hemoglobin", "haemoglobin", "hb",
       "hgb"),
    _l("6690-2", "Leukocytes [#/volume] in Blood", "wbc", "total leukocyte count", "tlc",
       "white blood cells", "total wbc count"),
    _l("777-3", "Platelets [#/volume] in Blood", "platelets", "platelet count", "plt"),
    _l("789-8", "Erythrocytes [#/volume] in Blood", "rbc", "red blood cells", "rbc count"),
    _l("4544-3", "Hematocrit [Volume Fraction] of Blood", "hematocrit", "pcv", "hct",
       "haematocrit"),
    _l("2951-2", "Sodium [Moles/volume] in Serum or Plasma", "sodium", "na", "serum sodium"),
    _l("2823-3", "Potassium [Moles/volume] in Serum or Plasma", "potassium", "k",
       "serum potassium"),
    _l("2075-0", "Chloride [Moles/volume] in Serum or Plasma", "chloride", "cl"),
    _l("1975-2", "Bilirubin.total [Mass/volume] in Serum or Plasma", "total bilirubin",
       "bilirubin", "s bilirubin"),
    _l("1968-7", "Bilirubin.direct [Mass/volume] in Serum or Plasma", "direct bilirubin",
       "conjugated bilirubin"),
    _l("1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma",
       "alt", "sgpt", "alanine aminotransferase"),
    _l("1920-8", "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma",
       "ast", "sgot", "aspartate aminotransferase"),
    _l("6768-6", "Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma", "alp",
       "alkaline phosphatase"),
    _l("1751-7", "Albumin [Mass/volume] in Serum or Plasma", "albumin", "serum albumin"),
    _l("2885-2", "Protein [Mass/volume] in Serum or Plasma", "total protein"),
    _l("3016-3", "Thyrotropin [Units/volume] in Serum or Plasma", "tsh",
       "thyroid stimulating hormone"),
    _l("3024-7", "Thyroxine (T4) free [Mass/volume] in Serum or Plasma", "free t4", "ft4"),
    _l("3053-6", "Triiodothyronine (T3) [Mass/volume] in Serum or Plasma", "t3",
       "total t3"),
    _l("3084-1", "Urate [Mass/volume] in Serum or Plasma", "uric acid", "serum uric acid"),
    _l("17861-6", "Calcium [Mass/volume] in Serum or Plasma", "calcium", "serum calcium"),
    _l("1989-3", "25-hydroxyvitamin D3 [Mass/volume] in Serum or Plasma", "vitamin d",
       "25 oh vitamin d", "vit d"),
    _l("2132-9", "Cobalamin (Vitamin B12) [Mass/volume] in Serum or Plasma", "vitamin b12",
       "b12", "vit b12"),
    _l("4537-7", "Erythrocyte sedimentation rate by Westergren method", "esr"),
    _l("1988-5", "C reactive protein [Mass/volume] in Serum or Plasma", "crp",
       "c reactive protein"),
)  # fmt: skip

VITALS = (
    _l("8480-6", "Systolic blood pressure", "systolic blood pressure", "sbp", "systolic bp"),
    _l("8462-4", "Diastolic blood pressure", "diastolic blood pressure", "dbp", "diastolic bp"),
    _l("85354-9", "Blood pressure panel with all children optional", "blood pressure", "bp"),
    _l("8867-4", "Heart rate", "heart rate", "pulse", "pulse rate", "hr"),
    _l("9279-1", "Respiratory rate", "respiratory rate", "rr", "resp rate"),
    _l("8310-5", "Body temperature", "body temperature", "temperature", "temp"),
    _l("59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry",
       "oxygen saturation", "spo2", "o2 saturation"),
    _l("29463-7", "Body weight", "body weight", "weight", "wt"),
    _l("8302-2", "Body height", "body height", "height", "ht"),
    _l("39156-5", "Body mass index (BMI) [Ratio]", "body mass index", "bmi"),
    _l("9843-4", "Head Occipital-frontal circumference", "head circumference", "ofc"),
    _l("8280-0", "Waist Circumference at umbilicus by Tape measure", "waist circumference"),
    _l("2339-0", "Glucose [Mass/volume] in Blood", "blood glucose", "capillary blood glucose",
       "cbg", "grbs"),
    _l("72514-3", "Pain severity - 0-10 verbal numeric rating [Score] - Reported",
       "pain score"),
)  # fmt: skip

DRUGS = (
    _c("372567009", "Metformin", "glycomet", "gluformin", "obimet"),
    _c("386864001", "Amlodipine", "amlong", "amlopres", "stamlo"),
    _c("373444002", "Atorvastatin", "atorva", "lipitor", "storvas", "atocor"),
    _c("387458008", "Aspirin", "ecosprin", "disprin", "acetylsalicylic acid"),
    _c("386952008", "Clopidogrel", "clopilet", "plavix", "deplatt"),
    _c("372826007", "Metoprolol", "metpure", "metolar", "betaloc", "seloken"),
    _c("373567002", "Losartan", "losar", "repace", "losacar"),
    _c("387069000", "Telmisartan", "telma", "telmikind", "telsartan"),
    _c("386872004", "Ramipril", "cardace"),
    _c("386966003", "Glimepiride", "amaryl", "glimy"),
    _c("387517004", "Paracetamol", "dolo", "crocin", "calpol", "acetaminophen", "pcm"),
    _c("395821003", "Pantoprazole", "pan", "pantocid", "pantop"),
    _c("387137007", "Omeprazole", "omez"),
    _c("372687004", "Amoxicillin", "mox", "novamox"),
    _c("387531004", "Azithromycin", "azithral", "azee"),
    _c("372897005", "Salbutamol", "asthalin", "albuterol"),
    _c("387475002", "Furosemide", "lasix", "frusemide"),
    _c("116601002", "Prednisolone", "wysolone", "omnacortil"),
    _c("372523007", "Cetirizine", "cetzine", "okacet"),
    _c("372487007", "Ondansetron", "emeset", "ondem"),
)  # fmt: skip

ALLERGENS = (
    _c("373270004", "Penicillin", "penicillins", "penicillin allergy"),
    _c("387458008", "Aspirin", "asa"),
    _c("387207008", "Ibuprofen", "brufen"),
    _c("256349002", "Peanut", "peanuts", "groundnut"),
    _c("111088007", "Latex", "natural rubber latex"),
    _c("102263004", "Eggs", "egg"),
)  # fmt: skip

ROUTE_SCT = {
    "oral": ("26643006", "Oral route"),
    "po": ("26643006", "Oral route"),
    "sc": ("34206005", "Subcutaneous route"),
    "subcutaneous": ("34206005", "Subcutaneous route"),
    "iv": ("47625008", "Intravenous route"),
    "intravenous": ("47625008", "Intravenous route"),
    "im": ("78421000", "Intramuscular route"),
    "intramuscular": ("78421000", "Intramuscular route"),
    "topical": ("6064005", "Topical route"),
    "sublingual": ("37839007", "Sublingual route"),
    "sl": ("37839007", "Sublingual route"),
    "rectal": ("37161004", "Rectal route"),
    "nasal": ("46713006", "Nasal route"),
}

# fact_type -> the concept tables it may bind to
TABLES_FOR_FACT_TYPE: dict[str, tuple[tuple[Concept, ...], ...]] = {
    "condition": (CONDITIONS,),
    "symptom": (SYMPTOMS,),
    "procedure": (PROCEDURES,),
    "lab_result": (LABS,),
    "vital_sign": (VITALS,),
    "medication": (DRUGS,),
    "allergy": (ALLERGENS, DRUGS),
}

ALL_CONCEPTS = CONDITIONS + SYMPTOMS + PROCEDURES + LABS + VITALS + DRUGS + ALLERGENS
