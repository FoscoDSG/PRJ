# Intelligent Enterprise Operating Layer

## Overview

Questo progetto descrive una piattaforma dati, semantica e AI pensata per diventare un vero **operating layer intelligente dell'azienda**.

L'obiettivo non è costruire un semplice chatbot, una dashboard o un sistema BI, ma una piattaforma capace di:

1. collegarsi alle fonti dati aziendali;
2. ingerire e normalizzare i dati;
3. arricchirli con AI;
4. costruire una rappresentazione semantica dell'azienda;
5. modellare oggetti, relazioni, eventi e regole tramite ontologia;
6. creare un knowledge graph operativo;
7. permettere ricerca e analytics avanzati;
8. utilizzare agenti AI specializzati;
9. monitorare costantemente flussi, KPI, anomalie e trend;
10. individuare problemi, rischi e opportunità;
11. fare root-cause analysis;
12. generare strategie operative;
13. simulare gli effetti delle strategie;
14. applicare policy e controlli;
15. richiedere approvazione umana quando necessario;
16. eseguire workflow reali su ERP, CRM, email e altri sistemi;
17. misurare i risultati;
18. imparare dal confronto tra impatto atteso e impatto reale.

La visione complessiva è:

```text
SOURCE
   ↓
INGESTION
   ↓
ETL
   ↓
AI ENRICHMENT
   ↓
GOVERNANCE
   ↓
ONTOLOGY
   ↓
KNOWLEDGE GRAPH
   ↓
SEARCH / ANALYTICS
   ↓
AI / AGENTS
   ↓
WORKFLOW
   ↓
ACTION
```

In realtà, la piattaforma non dovrebbe essere progettata come una semplice pipeline lineare, ma come un sistema modulare e circolare.

---

# 1. Architettura generale

```text
                  ┌─────────────────────┐
                  │     GOVERNANCE      │
                  │ security / lineage  │
                  │ permissions / audit │
                  └─────────────────────┘
                           │
                           ▼

SOURCES
   │
   ▼
CONNECTORS
   │
   ▼
DATA PLATFORM
 ingestion / ETL / quality
   │
   ├───────────────┐
   ▼               ▼
AI ENRICHMENT    RAW DATA
   │
   ▼
SEMANTIC LAYER
   │
   ▼
ONTOLOGY
   │
   ▼
KNOWLEDGE GRAPH
   │
   ├─────────────┬─────────────┐
   ▼             ▼             ▼
SEARCH        ANALYTICS      AI AGENTS
                                 │
                                 ▼
                          DECISION ENGINE
                                 │
                                 ▼
                           WORKFLOW ENGINE
                                 │
                                 ▼
                           ACTION LAYER
                                 │
                ┌────────────────┼───────────────┐
                ▼                ▼               ▼
               ERP              CRM            EMAIL
```

Governance, identity, security, logging, observability, versioning, permissions e audit non devono essere uno step isolato, ma un layer trasversale.

---

# 2. Sources

Le fonti possono comprendere:

```text
ERP
CRM
Database SQL
Excel
CSV
PDF
Email
API
sensori
IoT
macchinari
gestionali legacy
sistemi logistici
sistemi finanziari
file system
```

La piattaforma deve poter leggere dati strutturati e non strutturati.

---

# 3. Ingestion

Il layer di ingestion collega i sistemi sorgente alla piattaforma.

Esempio:

```text
SAP ─────┐
CRM ─────┤
Excel ───┤
Email ───┼──> CONNECTORS
PDF ─────┤
SQL ─────┘
```

Non è necessario costruire tutto da zero. Per molti connettori standard possono essere usati sistemi esistenti, mentre conviene sviluppare internamente solo i connettori strategici.

Nel lungo periodo è utile creare un **Connector SDK** per permettere a partner e clienti di costruire integrazioni aggiuntive.

---

# 4. ETL, normalizzazione e data quality

L'ETL serve a:

- normalizzare i dati;
- pulire errori;
- deduplicare;
- riconciliare valori;
- risolvere entità duplicate;
- controllare schema e consistenza.

Esempio:

```text
Cliente:
Mario Rossi Srl
M. Rossi SRL
ROSSI MARIO SRL
```

viene ricondotto a:

```text
customer_id = 18472
company_name = Mario Rossi S.r.l.
vat = ITXXXXXXXXXXX
```

Questa parte deve includere:

- entity resolution;
- data quality engine;
- master data management leggero;
- conflict resolution;
- source-of-truth management.

Esempio di source priority:

```text
Customer.address

Source priority:
1 ERP
2 CRM
3 manual update
4 AI inferred
```

---

# 5. AI Enrichment

L'AI trasforma contenuti non strutturati in dati utilizzabili.

Esempio:

```text
ordine_cliente_183.pdf
```

diventa:

```json
{
  "customer": "Acme Srl",
  "product": "Pompa X200",
  "quantity": 40,
  "delivery_date": "2026-11-10",
  "price": 18400
}
```

Oppure una email:

> Ci servirebbero altre 40 pompe X200 entro metà novembre.

viene trasformata nello stesso oggetto strutturato.

Questo permette di trattare in maniera uniforme:

```text
database
documenti
email
immagini
messaggi
```

---

# 6. Governance

Il sistema deve sapere:

- chi può vedere cosa;
- chi può modificare cosa;
- da dove arriva un dato;
- chi lo ha prodotto;
- come è stato trasformato;
- quando è stato aggiornato;
- quali azioni sono state eseguite.

Esempio:

```text
Dato
│
├── source: SAP
├── owner: Finance
├── sensitivity: confidential
├── updated: 14:32
├── access:
│      CFO
│      Finance
│
└── lineage:
       SAP → ETL → Customer → Revenue
```

La governance comprende:

```text
Security
Permissions
Audit
Data lineage
Versioning
Privacy
Model governance
Explainability
Human approval
Access control
Data residency
```

---

# 7. Ontologia

L'ontologia è il cuore semantico della piattaforma.

Non è semplicemente un database.

È il modello digitale del funzionamento dell'azienda.

Esempio di oggetti:

```text
CUSTOMER
ORDER
PRODUCT
SUPPLIER
FACTORY
INVOICE
PAYMENT
WAREHOUSE
SHIPMENT
EMPLOYEE
MACHINE
PRODUCTION BATCH
```

Esempio di relazioni:

```text
CUSTOMER
    │
    └── places
           ↓
         ORDER
           │
           ├── contains → PRODUCT
           │
           └── generates → INVOICE
                              │
                              └── settled_by → PAYMENT
```

L'ontologia deve modellare anche azioni:

```text
ORDER

properties
- amount
- customer
- status
- delivery_date

relationships
- customer
- products
- factory

actions
- approve
- reject
- modify
- send_to_ERP
- request_information
```

---

# 8. Ontologia pre-costruita e adattabile

Invece di costruire una nuova ontologia per ogni cliente, è preferibile utilizzare una struttura multilivello:

```text
CORE ONTOLOGY
   ↓
INDUSTRY ONTOLOGY
   ↓
PROCESS MODULES
   ↓
CUSTOMER EXTENSION
```

## 8.1 Core ontology

Concetti molto generali:

```text
Person
Organization
Asset
Document
Event
Location
Transaction
```

## 8.2 Industry ontology

Esempio manufacturing:

```text
Customer
Supplier
Order
Product
Warehouse
ProductionBatch
Machine
Shipment
Invoice
Payment
```

## 8.3 Process ontology

Moduli riutilizzabili:

```text
Sales
Procurement
Inventory
Finance
Manufacturing
Logistics
Maintenance
Quality
```

## 8.4 Customer extension

Aggiunge solo le specificità del singolo cliente.

Esempio:

```text
BASE ONTOLOGY

Order
Customer
Product
Supplier
Invoice

        +

CUSTOMER A EXTENSION

Order → Commessa
Customer → Cliente
Product → Articolo

additional objects:
- Agente
- AreaCommerciale
- LottoProduzione
```

Il template base non dovrebbe mai essere modificato direttamente.

---

# 9. Auto-Mapping Engine

Una funzione chiave è l'adattamento automatico dell'ontologia ai dati del cliente.

Il sistema analizza:

```text
SAP tables
CRM schema
Excel columns
API
PDF
email
```

e propone mapping:

```text
"ANAG_CLIENTI" → Customer
"ORD_HEAD" → Order
"COD_ART" → Product
"FORN" → Supplier
```

con confidence score:

```text
ORD_HEAD → Order      98%
ANAG_CLI → Customer   96%
COD_ART → Product     99%
DOC_01 → ???          42%
```

I casi ambigui vengono passati all'utente.

Obiettivo:

```text
connect data
↓
AI scans schema
↓
pre-built ontology selected
↓
automatic mapping
↓
human review
↓
go-live
```

Questo riduce il deployment e il lock-in consulenziale.

---

# 10. Schema discovery e schema drift

La piattaforma deve:

- leggere tabelle, colonne e chiavi;
- inferire relazioni;
- capire frequenze di aggiornamento;
- identificare nuovi campi;
- rilevare modifiche;
- proporre correzioni.

Quando il cliente cambia ERP, rinomina una colonna o aggiunge una tabella, il sistema deve rilevare il **schema drift** e aggiornare il mapping senza rompere la piattaforma.

---

# 11. Ontology versioning

L'ontologia deve supportare:

```text
Ontology v1.2
Ontology v1.3
Ontology v2.0
```

con:

- changelog;
- rollback;
- migrazioni controllate;
- confronto tra versioni;
- gestione estensioni cliente.

---

# 12. Business Rules Engine

L'ontologia deve rappresentare anche regole operative.

Esempio:

```text
IF order_amount > €50,000
THEN CFO approval required
```

Le regole devono essere separate dalle istruzioni degli agenti AI.

---

# 13. Calculated properties

Alcune proprietà non arrivano direttamente dalle fonti, ma vengono calcolate.

Esempi:

```text
CustomerRisk
LateDeliveryProbability
AvailableToPromise
SupplierReliability
OrderMargin
ProductionEfficiency
```

---

# 14. Event model

La piattaforma deve modellare anche eventi:

```text
ordine creato
pagamento ricevuto
macchina guasta
spedizione partita
documento modificato
ordine cancellato
approvazione ricevuta
```

Gli eventi alimentano agenti, workflow e analisi temporali.

---

# 15. Temporal ontology

Il sistema non deve sapere solo lo stato corrente, ma anche come era la situazione nel passato.

Domanda possibile:

> Perché questo ordine era classificato ad alto rischio martedì scorso?

Questo richiede versionamento temporale di oggetti, relazioni e proprietà.

---

# 16. Provenance e confidence

Ogni informazione deve distinguere:

- dato originale;
- dato trasformato;
- dato inferito dall'AI;
- dato approvato dall'umano.

Esempio:

```text
SupplierRisk = HIGH

source:
- 3 ritardi ERP
- email del fornitore
- previsione AI

confidence: 87%
```

---

# 17. Knowledge Graph

L'ontologia definisce il modello.

Il knowledge graph contiene le istanze reali.

Esempio:

```text
        ACME SRL
           │
        ordered
           ↓
       ORDER 1024
        /        \
       ↓          ↓
 PRODUCT X     PRODUCT Y
     │
     ↓
 SUPPLIER A
```

Il sistema può quindi ragionare sulle relazioni tra dati e non soltanto sui dati stessi.

---

# 18. Search e Analytics

La piattaforma deve supportare:

## Dashboard tradizionali

```text
Revenue
Orders
Inventory
Customers
Suppliers
```

## Ricerca naturale

Esempio:

> Mostrami gli ordini superiori a 50.000 € con ritardo superiore a 10 giorni che dipendono da fornitori con problemi di consegna.

Il sistema deve tradurre la richiesta attraverso l'ontologia e scegliere il motore corretto.

---

# 19. Semantic search + graph search

Non basta un vector database.

Una query può richiedere:

- SQL;
- graph database;
- document store;
- vector search;
- API.

Esempio:

> Quali clienti con ordini superiori a €100k hanno ricevuto reclami negli ultimi 90 giorni?

Qui il knowledge graph è più importante del puro RAG.

---

# 20. Query Planner AI

Il sistema decide automaticamente dove cercare.

```text
User question
     ↓
Query planner
 ┌────┼─────┐
SQL Graph Vector
 └────┼─────┘
      ↓
 answer
```

---

# 21. Business Object Pages

Ogni entità dovrebbe avere una pagina operativa.

Esempio:

```text
CUSTOMER: ACME SRL

Revenue
€4.2M

Orders
42

Open invoices
€320k

Risk
Medium

Related:
→ 14 products
→ 3 sales reps
→ 2 warehouses
→ 7 complaints
```

---

# 22. AI Agents

Gli agenti non devono essere un semplice chatbot.

Possibili agenti:

```text
Procurement Agent
Finance Agent
Sales Agent
Operations Agent
Risk Agent
Production Agent
Logistics Agent
Quality Agent
Fleet Agent
Supplier Agent
Inventory Agent
```

Ogni agente deve conoscere una parte dell'ontologia.

---

# 23. Esempio Procurement Agent

Ogni mattina analizza:

```text
orders
inventory
suppliers
production
delivery dates
```

trova:

```text
Order #18472

Delivery risk: HIGH

Reason:
Supplier A delayed shipment
Inventory insufficient
Production scheduled in 4 days
```

e propone:

```text
1. Increase Supplier B order
2. Move production batch
3. Notify customer
```

---

# 24. Workflow

L'agente non deve modificare direttamente i sistemi senza controllo.

Flusso:

```text
AI detects problem
       ↓
creates recommendation
       ↓
manager approval
       ↓
workflow engine
       ↓
execution
```

Esempio:

```text
Supplier delay detected

        ↓

AI proposes:
"Order 200 units from Supplier B"

        ↓

Procurement manager

[APPROVE]
[MODIFY]
[REJECT]

        ↓

ERP updated
```

---

# 25. Action Layer

L'obiettivo è andare oltre la BI.

```text
DATA
 ↓
UNDERSTANDING
 ↓
DECISION
 ↓
ACTION
```

Azioni possibili:

```text
ERP:
createOrder()

ERP:
createPurchaseOrder()

CRM:
updateCustomer()

Email:
sendConfirmation()
```

---

# 26. Closed Loop

L'azione deve diventare un nuovo evento nel sistema.

```text
SOURCE → DATA → KNOWLEDGE → AI → ACTION
  ↑                                      │
  └──────────────────────────────────────┘
```

Questa è una delle differenze fondamentali rispetto a un normale sistema BI.

---

# 27. Human-in-the-loop

L'autonomia deve essere progressiva.

```text
READ
↓
SUGGEST
↓
APPROVAL REQUIRED
↓
AUTO EXECUTE
```

L'AI deve sapere quando è necessario chiedere conferma.

---

# 28. Action permissions

Le autorizzazioni devono esistere anche a livello di azione.

Un agente può:

```text
read SAP
```

ma non necessariamente:

```text
create payment
```

---

# 29. Policy Engine

Le policy devono essere separate dal prompt degli agenti.

Esempio:

```text
AI agent wants:
createPurchaseOrder(€120,000)

Policy engine:
amount > €50,000

→ Human approval required
```

---

# 30. Dry-run / Simulation Mode

Prima dell'esecuzione, il sistema deve poter simulare le conseguenze.

```text
SIMULATE

Create purchase order
Supplier: Alfa
Amount: €82,000

Effects:
Inventory +500
Cash commitment +€82,000
Expected delivery: 12 Nov
```

---

# 31. Rollback

Dove tecnicamente possibile, deve essere possibile annullare operazioni automatiche.

---

# 32. Agent Audit Trail

Ogni decisione deve essere ricostruibile.

```text
10:43 Agent detected anomaly
10:43 Read ERP order #4812
10:44 Checked warehouse
10:44 Proposed supplier change
10:46 Approved by Mario
10:46 ERP updated
```

---

# 33. Explainability

L'utente deve poter chiedere:

> Perché hai preso questa decisione?

La risposta deve essere basata sui dati realmente utilizzati.

---

# 34. Agent Memory

La memoria degli agenti deve essere controllata.

Separare:

```text
temporary memory
customer memory
verified knowledge base
```

---

# 35. Context Builder

Prima di interrogare un LLM, il sistema deve costruire automaticamente il contesto minimo necessario a partire dal knowledge graph.

Questo riduce:

- costi;
- hallucination;
- contesto inutile;
- errori.

---

# 36. Workflow Builder No-Code

Gli utenti devono poter creare workflow senza sviluppatori.

```text
WHEN
invoice overdue > 30 days

IF
amount > €20,000

THEN
notify CFO

AND
create collection task
```

---

# 37. Natural-language workflow creation

L'utente può dire:

> Quando una fattura supera 30 giorni di ritardo e vale più di 20 mila euro, avvisa il CFO.

Il sistema genera il workflow e chiede conferma.

---

# 38. Exception Management

Il sistema deve automatizzare il normale e portare all'umano solo le eccezioni.

```text
1.000 orders

930 automatically processed
45 require confirmation
20 missing information
5 suspicious
```

KPI chiave:

> Quanti casi umani riesce a eliminare il sistema?

---

# 39. Exception Inbox

Interfaccia centrata su ciò che necessita attenzione.

```text
TODAY

12 supplier issues
7 order anomalies
4 invoices requiring approval
2 high-risk customers
```

---

# 40. Prioritization Engine

Le eccezioni devono essere ordinate per:

- impatto economico;
- urgenza;
- rischio;
- criticità operativa.

---

# 41. Operational Digital Twin

Il knowledge graph può diventare un modello operativo vivo dell'azienda.

Domanda:

> Cosa succede se Supplier A smette di consegnare per 15 giorni?

Il sistema attraversa:

```text
Supplier A
↓
Products
↓
Orders
↓
Customers
↓
Revenue at risk
```

---

# 42. Impact Analysis

Prima di applicare una modifica, il sistema mostra quali oggetti, processi e KPI vengono toccati.

---

# 43. Root-Cause Analysis

Il sistema non deve limitarsi a identificare un problema.

Deve cercare la catena causale:

```text
Delivery delay
↓
production delay
↓
component unavailable
↓
Supplier B late
↓
purchase order approved 4 days late
```

---

# 44. Intelligent Alerts

Gli alert correlati devono essere raggruppati per evitare centinaia di notifiche.

---

# 45. Operational KPIs

La piattaforma dovrebbe generare automaticamente KPI a partire dall'ontologia.

---

# 46. Auto-generated dashboards

Una volta riconosciuti Customer, Order, Invoice, Supplier, ecc., il sistema può generare automaticamente una prima dashboard operativa.

---

# 47. Benchmark interno

La piattaforma può confrontare:

- filiali;
- fornitori;
- stabilimenti;
- reparti;
- team;
- periodi temporali.

---

# 48. Connector SDK

I partner devono poter creare nuovi connector.

---

# 49. Action SDK

I partner devono poter aggiungere azioni.

Esempio:

```text
SAP.createOrder()
SAP.cancelOrder()
SAP.updateSupplier()
```

---

# 50. Ontology SDK

Partner e software house devono poter creare vertical ontology modules.

---

# 51. Marketplace di moduli

Possibili pacchetti:

```text
Manufacturing Pack
Procurement Pack
Finance Pack
Logistics Pack
Construction Pack
Retail Pack
```

---

# 52. Multi-Agent Orchestration

Invece di un solo super-agente:

```text
Procurement Agent
Finance Agent
Sales Agent
Operations Agent
Compliance Agent
```

con un orchestratore centrale.

---

# 53. Agent Evaluation

Gli agenti devono essere misurati.

Metriche:

- accuracy;
- error rate;
- escalation rate;
- cost per action;
- correct action rate;
- human override;
- outcome quality.

---

# 54. Shadow Mode

Prima di concedere autonomia, l'agente deve lavorare senza eseguire nulla.

Le sue decisioni vengono confrontate con quelle degli operatori umani.

---

# 55. Progressive Autonomy

```text
Level 0 → read only
Level 1 → suggestions
Level 2 → actions with approval
Level 3 → autonomous low-risk actions
Level 4 → autonomous workflows
```

---

# 56. Cost Control AI

Il sistema deve fare routing automatico tra modelli.

Non tutte le operazioni richiedono il modello più costoso.

---

# 57. Model-Agnostic Architecture

La piattaforma dovrebbe poter utilizzare diversi provider AI e modelli locali senza dipendere da uno solo.

---

# 58. On-Prem / Private Cloud

Importante per:

- grandi enterprise;
- difesa;
- sanità;
- settori regolamentati;
- dati sensibili.

---

# 59. Data Residency

La piattaforma deve sapere dove vengono memorizzati i dati e consentire configurazioni UE/Italia.

---

# 60. Fine-Grained Permissions

Esempio:

```text
Sales:
Customer.name ✓
Customer.orders ✓
Customer.margin ✗
Customer.creditRisk ✗
```

---

# 61. Field-Level Masking

I singoli campi sensibili devono poter essere mascherati.

---

# 62. Immutable Audit Log

Per le operazioni critiche è utile un registro non modificabile.

---

# 63. Natural Language Ontology Editor

L'utente può dire:

> Da noi un cliente può avere più stabilimenti e ogni stabilimento ha un responsabile.

Il sistema propone la modifica all'ontologia.

---

# 64. Ontology Recommendation Engine

Il sistema osserva i dati e suggerisce nuovi oggetti e relazioni.

Esempio:

> Il campo Project_ID compare nel 63% degli ordini. Vuoi creare l'oggetto Project?

---

# 65. Ontology Pruning

Gli oggetti inutilizzati possono essere nascosti o rimossi su suggerimento del sistema.

---

# 66. Learning from Corrections

Le correzioni dei clienti devono migliorare i futuri deployment.

Esempio:

```text
FIELD "COD_ART"
→ Product.code

FIELD "RAG_SOC"
→ Organization.name

FIELD "PIVA"
→ Organization.VAT_ID
```

Dopo molti clienti dello stesso settore, il mapping diventa sempre più affidabile.

---

# 67. Business Optimizer

Uno dei moduli più interessanti è un layer di **continuous intelligence**.

Gli agenti non vengono usati solo quando l'utente fa una domanda.

Monitorano continuamente:

- produzione;
- logistica;
- inventory;
- fornitori;
- qualità;
- costi;
- vendite;
- cash flow;
- manutenzione;
- KPI;
- trend;
- anomalie;
- eventi.

Architettura:

```text
DATA + EVENTS + ONTOLOGY
        ↓
MONITORING AGENTS
        ↓
ANOMALY / TREND DETECTION
        ↓
ROOT CAUSE ANALYSIS
        ↓
STRATEGY GENERATION
        ↓
SIMULATION
        ↓
EXPECTED IMPACT
        ↓
HUMAN APPROVAL
        ↓
WORKFLOW / ACTION
        ↓
RESULT MEASUREMENT
        ↓
LEARNING
```

---

# 68. Transportation Agent

Può osservare:

```text
tempi medi di consegna
ritardi per tratta
costo/km
saturazione mezzi
traffico
disponibilità vettori
ordini futuri
SLA clienti
```

Esempio:

```text
Le consegne Nord-Est hanno +18% di ritardi
nelle ultime 6 settimane.
```

Root-cause analysis:

```text
Cause probabili:

41% → tratte concentrate lunedì
32% → capacità insufficiente vettore A
19% → tempi di carico stabilimento Verona
8%  → altre cause
```

Strategia:

```text
STRATEGIA PROPOSTA

1. Spostare il 22% delle spedizioni dal lunedì al martedì.
2. Assegnare parte del volume da vettore A a vettore B.
3. Anticipare il cut-off di carico di 45 minuti.

Impatto stimato:
- ritardi: -27%
- costo trasporto: +1,8%
- SLA rispettati: +11%
```

---

# 69. Production Agent

Può osservare:

```text
machines
production lines
orders
inventory
operators
maintenance
suppliers
energy
quality
scrap
downtime
```

Esempio:

> La Linea 3 perde mediamente 52 minuti al giorno tra le 14:00 e le 17:00 quando vengono lavorati contemporaneamente Product A e Product C.

Il knowledge graph può mostrare:

```text
Product A
      \
       → Machine M14
      /
Product C
```

Strategia:

```text
Separare i due batch.

A → mattina
C → pomeriggio
```

Simulazione:

```text
CURRENT
Throughput: 1.820 units/day
Downtime: 74 min
Cost/unit: €4,38

PROPOSED
Throughput: 2.040 units/day
Downtime: 29 min
Cost/unit: €4,12
```

---

# 70. Gerarchia di agenti

```text
                BUSINESS OPTIMIZER
                       │
        ┌──────────────┼───────────────┐
        ↓              ↓               ↓
Production Agent   Logistics Agent   Procurement Agent
        ↓              ↓               ↓
Quality Agent      Fleet Agent       Supplier Agent
```

Sopra di loro può esserci un **Strategic Agent** o orchestratore.

---

# 71. Risoluzione dei conflitti tra agenti

Gli agenti possono avere obiettivi differenti.

Esempio:

Production Agent:

> Aumenta il batch size.

Inventory Agent:

> Questo aumenterebbe lo stock medio del 21%.

Finance Agent:

> Immobilizzerebbe €620.000 di capitale.

Il Business Optimizer deve trovare il compromesso tra:

```text
production efficiency
inventory
cash
service level
risk
```

---

# 72. Tre livelli di intelligence

## Operational intelligence

Orizzonte: ore/giorni.

Esempio:

> Sposta questa produzione dalla Linea 2 alla Linea 4.

## Tactical intelligence

Orizzonte: settimane/mesi.

Esempio:

> Il vettore B sta peggiorando progressivamente; sposta il 15% dei volumi.

## Strategic intelligence

Orizzonte: mesi/anni.

Esempio:

> La capacità dello stabilimento Nord raggiungerà il 94% entro 9 mesi. Considerando crescita ordini, downtime e lead time fornitori, conviene valutare una seconda linea.

---

# 73. Strategy Object

Le strategie devono essere oggetti dell'ontologia.

```text
Strategy

problem
objective
hypothesis
actions
expected_impact
cost
risk
confidence
time_horizon
affected_objects
owner
status
actual_result
```

Esempio:

```text
Strategy #184

Objective:
Reduce delivery delay

Expected impact:
-18%

Cost:
€42k/year

Risk:
Low

Confidence:
82%

Status:
Pilot
```

---

# 74. Strategy Learning Loop

```text
AI PROPOSES STRATEGY
        ↓
COMPANY IMPLEMENTS
        ↓
SYSTEM MEASURES RESULTS
        ↓
EXPECTED vs ACTUAL
        ↓
MODEL LEARNS
```

Esempio:

```text
Predicted savings: €380k
Actual savings: €310k

Prediction error: 18.4%
```

---

# 75. Strategy Library

Con il tempo la piattaforma può costruire:

```text
Industry Ontology Library
+
Operational Pattern Library
+
Strategy Library
+
AI Agents
```

Esempi di pattern:

```text
problema:
supplier concentration

strategia:
dual sourcing
```

oppure:

```text
problema:
warehouse congestion

strategia:
dynamic slot allocation
```

---

# 76. Home Page ideale

La home non dovrebbe essere un chatbot.

Potrebbe essere:

```text
┌──────────────────────────────────────────┐
│          COMPANY INTELLIGENCE            │
│                                          │
│ Potential annual improvement             │
│              € 1.82M                     │
│                                          │
├──────────────────────────────────────────┤
│                                          │
│ OPPORTUNITIES                     17     │
│ CRITICAL RISKS                     3     │
│ STRATEGIES READY                   8     │
│ STRATEGIES RUNNING                 4     │
│ HUMAN DECISIONS REQUIRED           5     │
│                                          │
├──────────────────────────────────────────┤
│                                          │
│ TOP OPPORTUNITIES                        │
│                                          │
│ Production scheduling       €420k        │
│ Inventory optimization      €370k        │
│ Logistics consolidation     €280k        │
│ Supplier optimization       €190k        │
│ Energy optimization         €120k        │
│                                          │
└──────────────────────────────────────────┘
```

Quando il dirigente entra in un'opportunità vede:

```text
PERCHÉ?
↓
quali dati
↓
quale problema
↓
quale causa
↓
quale strategia propone l'AI
↓
quanto dovrebbe rendere
↓
quali rischi comporta
↓
simulazione
↓
APPROVE
```

---

# 77. Sintesi completa

```text
                           AZIENDA
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                       1. DATA SOURCES                        │
│ ERP • CRM • Database • Excel • Email • PDF • API • IoT     │
│ macchinari • logistica • fornitori • ordini • pagamenti     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                     2. INGESTION LAYER                       │
│                                                              │
│ Connectors raccolgono continuamente dati e nuovi eventi.     │
│                                                              │
│ SAP • Salesforce • SQL • REST API • Email • File System     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                3. DATA CLEANING / ETL / QUALITY              │
│                                                              │
│ • normalizzazione                                            │
│ • deduplicazione                                             │
│ • controllo errori                                           │
│ • entity resolution                                          │
│ • riconciliazione tra fonti                                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                     4. AI ENRICHMENT                         │
│                                                              │
│ email / PDF / documenti / immagini                           │
│                ↓                                             │
│ Customer • Order • Product • Amount • Date • Supplier       │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    SCHEMA DISCOVERY AI
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                  5. ONTOLOGY MAPPING ENGINE                  │
│                                                              │
│ ANAG_CLI ───────→ Customer          98%                     │
│ COD_ART ────────→ Product           99%                     │
│ ORD_TEST ───────→ Order             96%                     │
│ XYZ_004 ────────→ ???               37% → human review     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                       6. ONTOLOGY                            │
│                                                              │
│ CORE ONTOLOGY                                                │
│ Person • Organization • Asset • Event • Document            │
│                                                              │
│            +                                                 │
│                                                              │
│ INDUSTRY ONTOLOGY                                            │
│ Customer • Supplier • Product • Order • Warehouse           │
│                                                              │
│            +                                                 │
│                                                              │
│ PROCESS MODULES                                              │
│ Sales • Procurement • Logistics • Finance • Manufacturing   │
│                                                              │
│            +                                                 │
│                                                              │
│ CUSTOMER EXTENSION                                           │
│ concetti e processi specifici dell'azienda                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                   7. KNOWLEDGE GRAPH                         │
│                                                              │
│ Tutta l'azienda diventa una rete di oggetti e relazioni.     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
             ┌────────────────────────────────┐
             │  DIGITAL MODEL OF THE COMPANY │
             └────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          SEARCH          ANALYTICS       AI / AGENTS
```

Seconda parte:

```text
                        KNOWLEDGE GRAPH
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    8. BUSINESS MONITORING                    │
│                                                              │
│ • produzione                                                 │
│ • logistica                                                  │
│ • inventario                                                 │
│ • fornitori                                                  │
│ • qualità                                                    │
│ • costi                                                      │
│ • vendite                                                    │
│ • cash flow                                                  │
│ • manutenzione                                               │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                 9. ANOMALY / TREND DETECTION                │
│                                                              │
│ anomalie • inefficienze • trend • rischi • opportunità      │
│ colli di bottiglia • sprechi • capacità inutilizzata         │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    10. ROOT CAUSE ANALYSIS                   │
│                                                              │
│ Il sistema cerca di capire PERCHÉ.                           │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    BUSINESS OPTIMIZER
                              │
                              ▼
                    STRATEGY GENERATION
                              │
                              ▼
                       SIMULATION
                              │
                              ▼
                       POLICY ENGINE
                              │
                              ▼
                     HUMAN APPROVAL
                              │
                              ▼
                       WORKFLOW ENGINE
                              │
                              ▼
                         ACTION
                              │
                              ▼
                      MEASURE RESULT
                              │
                              ▼
                          LEARN
                              │
                              └─────────────→ KNOWLEDGE GRAPH
```

---

# 78. V1 consigliata

Non conviene partire cercando di costruire tutto.

Una V1 può essere:

```text
SOURCES
   ↓
CONNECTORS
   ↓
DATA NORMALIZATION
   ↓
AI ENRICHMENT
   ↓
AUTO-ONTOLOGY
   ↓
KNOWLEDGE GRAPH
   ↓
AI AGENT
   ↓
WORKFLOW
   ↓
ACTION
```

Con pochi connector iniziali:

```text
SQL
Excel/CSV
REST API
Email
ERP
```

e un solo vertical use case.

Esempio:

```text
ORDER MANAGEMENT
```

Input:

```text
email ordine
PDF ordine
ERP
magazzino
anagrafica cliente
```

Output:

```text
Customer
Order
Product
Warehouse
Supplier
```

L'agente può arrivare a:

> Ho ricevuto l'ordine di Acme per 500 unità di X.  
> Il cliente esiste nell'ERP.  
> Il prezzo coincide con il listino.  
> Sono disponibili 420 unità.  
> Mancano 80 unità.  
> Supplier B può consegnarle entro la data richiesta.  
> Vuoi creare l'ordine e generare l'approvvigionamento?

Dopo approvazione:

```text
ERP:
createOrder()

ERP:
createPurchaseOrder()

CRM:
updateCustomer()

Email:
sendConfirmation()
```

---

# 79. Elementi più strategici del prodotto

Cinque elementi possono distinguere davvero la piattaforma:

1. **pre-built composable ontology**
2. **automatic ontology mapping**
3. **automatic maintenance quando i dati cambiano**
4. **exception-centric workflows**
5. **safe AI → action layer con governance e progressive autonomy**

La parte realmente difendibile non è fare ETL, RAG o un agente LLM.

Il vantaggio competitivo può derivare da:

```text
source
→ auto-mapping
→ ontology
→ knowledge graph
→ continuous agents
→ strategy
→ simulation
→ action
→ learning
```

---

# 80. Visione finale

ERP:

> registra ciò che accade

BI:

> mostra ciò che è accaduto

AI Copilot:

> spiega ciò che è accaduto

Questa piattaforma:

```text
comprende
→ monitora
→ individua
→ spiega
→ propone
→ simula
→ approva
→ agisce
→ misura
→ impara
```

In una frase:

> **Il sistema collega i dati, comprende l'azienda, costruisce il suo modello digitale, osserva continuamente come sta funzionando, individua problemi e opportunità, sviluppa strategie, le simula, chiede approvazione quando necessario, le esegue, misura i risultati e impara.**

Questo è il nucleo dell'**Intelligent Enterprise Operating Layer**.
