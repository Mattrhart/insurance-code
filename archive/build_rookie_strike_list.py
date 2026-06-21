import pandas as pd
from datetime import datetime, timedelta

file_path = 'AllValidLicensesIndividual.csv'
output_file = 'rookie_strike_list_clean.csv'

print("Loading the 1.2M row database...")
df = pd.read_csv(file_path, low_memory=False)

print("Converting timelines...")
df['License Issue Date'] = pd.to_datetime(df['License Issue Date'], errors='coerce')

# THE STRATEGIC PIVOT: Target agents licensed 6 to 12 months ago
# Based on current date: June 20, 2026
today = datetime(2026, 6, 20)
six_months_ago = today - timedelta(days=180)
twelve_months_ago = today - timedelta(days=365)

print(f"Isolating agents licensed between {twelve_months_ago.strftime('%Y-%m-%d')} and {six_months_ago.strftime('%Y-%m-%d')}...")
timeline_filter = (df['License Issue Date'] >= twelve_months_ago) & (df['License Issue Date'] <= six_months_ago)
df_time = df[timeline_filter]

print("Filtering for Life/Annuity operators...")
life_filter = df_time['License TYCL Desc'].str.contains('Life', case=False, na=False)
df_life = df_time[life_filter]

print("Removing targets with missing contact vectors...")
df_actionable = df_life.dropna(subset=['Email Address', 'Business Phone'], how='all').copy()

print("Scrubbing phone data...")
df_actionable['Business Phone'] = df_actionable['Business Phone'].astype(str).str.replace(r'\D+', '', regex=True)

print("Executing deduplication...")
df_unique = df_actionable.drop_duplicates(subset=['First Name', 'Last Name', 'Email Address'])

# Sort by newest within that window so we get the ones hitting the 6-month wall right now
df_sorted = df_unique.sort_values(by='License Issue Date', ascending=False)
freshest_targets = df_sorted.head(1000)

columns_to_keep = ['First Name', 'Last Name', 'License Issue Date', 'Email Address', 'Business Phone', 'License TYCL Desc']
freshest_targets[columns_to_keep].to_csv(output_file, index=False)

print(f"\nTarget vector shifted. 1,000 starving agents exported to {output_file}.")
