# ConsultBae Data Ingestion Pipeline

This project is a data engineering pipeline that cleans and merges candidate/worker data from three disparate sources (Naukri applicants, Gig workers, and CBNexus contacts) into a unified PostgreSQL database.

## System Flow

1. **Step 1: Data Cleaning (`clean_data.py`)**
   * Reads raw CSVs from the `source/` directory.
   * Normalizes emails to lowercase and removes whitespace.
   * Standardizes phone numbers to a 10-digit format.
   * Maps cities to canonical names (e.g., Gurugram/Gurgaon to Gurgaon).
   * Parses multiple date formats into a standard `YYYY-MM-DD` format.
   * Standardizes skill tags by lowercasing and sorting them.
   * Drops blank rows and corrupt/column-shifted rows.
   * Writes the cleaned intermediate files to the `output/` directory.

2. **Step 2: Entity Merging and Database Load (`merge_data.py`)**
   * Recreates the target database schema in PostgreSQL.
   * Bulk-inserts the cleaned individual source tables.
   * Iterates through the data to merge records matching by "phone OR email".
   * Combines properties (such as unioning skills).
   * Saves the merged deduplicated entities to the `persons` master table.
   * Populates the `person_sources` link table to maintain source-to-target lineage.

## Key Decisions

* **Entity Resolution Strategy**: We match records if they share the same normalized phone number OR normalized email. When a conflict occurs (where a record has a phone matching one person but an email matching another), we trust the phone match.
* **No Raw File Mutation**: Original raw files are kept read-only; cleaned datasets are outputted separately to maintain idempotency and auditability.
* **Schema Lineage**: We maintain a dedicated junction table (`person_sources`) containing the foreign key `person_id`, source name, and raw row index to track exactly where each data point in the merged profile originated.
* **Data Casing and Types**: We load phone numbers explicitly as strings to prevent loss of leading zeros. We map verified flags directly to PostgreSQL booleans instead of numeric representations.

## How to Run

### 1. Prerequisites
Ensure you have Python 3.10+ and a running PostgreSQL database.

### 2. Setup
Clone or navigate to the project directory and install the required dependencies:
```bash
pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the root directory and add your PostgreSQL database URL:
```env
DATABASE_URL=postgresql://username:password@hostname:port/dbname
```

### 4. Execute the Pipeline
Run the data cleaning script first:
```bash
python clean_data.py
```

Then run the merge and database load script:
```bash
python merge_data.py
```
