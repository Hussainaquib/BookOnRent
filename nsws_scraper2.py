import time
import csv
import re
import random
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# ==========================================
# CONFIGURATION
# ==========================================
BASE_URL = "https://www.nsws.gov.in/portal/approvalsandregistrations"
OUTPUT_DIR = Path("nsws_data_slowed")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Headless = False allows you to see the browser and verify it's loading correctly
HEADLESS_MODE = False 

# ==========================================
# CSV HEADERS
# ==========================================
MINISTRY_HEADERS = [
    "Approval Link", "Approval Name", "Ministry Name", "About this approval",
    "Who can apply", "Documents required", "Approval applicability/trigger",
    "Application Fee", "Validity", "Average Time taken to get this", "Can be applied through NSWS"
]

DEPT_HEADERS = [
    "Approval Link", "Approval Name", "Department Name", "About this approval",
    "Who can apply", "Documents required", "Approval applicability/trigger",
    "Application Fee", "Validity", "Average Time taken to get this", "Can be applied through NSWS"
]

ALL_HEADERS = [
    "Approval Link", "Approval Name", "Ministry Name", "Department Name",
    "About this approval", "Who can apply", "Documents required",
    "Approval applicability/trigger", "Application Fee", "Validity",
    "Average Time taken to get this", "Can be applied through NSWS"
]

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def clean_text(text):
    if not text:
        return "N/A"
    # Remove HTML tags and collapse whitespace
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def write_to_csv(filepath, headers, row_data):
    file_exists = filepath.exists()
    with open(filepath, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_data)

def safe_extract_text(page, selector):
    """Safely extracts text from a selector, returns 'N/A' if missing."""
    try:
        if page.locator(selector).count() > 0:
            return clean_text(page.locator(selector).first.inner_text())
    except:
        pass
    return "N/A"

def extract_detail_page(context, url):
    """
    Opens the detail URL, WAITS significantly for data to populate, 
    and extracts fields.
    """
    page = context.new_page()
    data = {}
    
    try:
        print(f"      Opening: {url}")
        page.goto(url, timeout=60000)
        
        # --- CRITICAL SLOW DOWN: WAIT FOR NETWORK IDLE ---
        # This ensures all background API calls (XHR) are finished.
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass # Continue if network never goes fully idle (e.g. background analytics)

        # --- CRITICAL SLOW DOWN: EXPLICIT SLEEP ---
        # Wait 3 to 5 seconds to ensure React/Angular has rendered the text
        time.sleep(random.uniform(3, 5))
        
        # --- 1. Basic Info ---
        data["Approval Link"] = url
        
        # Name
        h1 = page.locator("h1").first
        if h1.count() == 0:
            # Retry wait if H1 isn't there yet
            time.sleep(2)
        data["Approval Name"] = clean_text(h1.inner_text()) if h1.count() else "N/A"

        # Ministry / Department (Scraped from Banner)
        banner_text = safe_extract_text(page, ".banner-content, .approval-banner")
        data["Ministry Name"] = "N/A"
        data["Department Name"] = "N/A"
        
        if banner_text != "N/A":
            lines = [l.strip() for l in banner_text.split('\n') if l.strip()]
            for line in lines:
                if "Ministry" in line:
                    data["Ministry Name"] = line
                if "Department" in line:
                    data["Department Name"] = line

        # --- 2. Content Sections (Robust Keyword Search) ---
        # We search for headers containing keywords, then get the content immediately after.
        
        def get_section_text(header_keywords):
            for keyword in header_keywords:
                # Look for H2/H3/Strong tags containing the keyword
                # This is more robust than class names
                xpath = f"//*[self::h2 or self::h3 or self::h4 or self::strong or self::b][contains(text(), '{keyword}')]"
                header = page.locator(xpath).first
                if header.count() > 0:
                    # Get the next sibling element (usually a div or p containing the text)
                    try:
                        content = page.locator(xpath + "/following-sibling::div[1] | " + xpath + "/following-sibling::p[1]").first
                        if content.count():
                             return clean_text(content.inner_text())
                        # Sometimes it's inside a parent wrapper, try going up and finding the description
                        content_parent = page.locator(xpath + "/..").locator("div").last
                        return clean_text(content_parent.inner_text())
                    except:
                        return "N/A"
            return "N/A"

        data["About this approval"] = get_section_text(["About", "Brief", "Description", "Objective"])
        data["Who can apply"] = get_section_text(["Who can", "Eligibility", "Beneficiary"])
        data["Documents required"] = get_section_text(["Documents", "Enclosures", "Attachment", "Checklist"])
        data["Approval applicability/trigger"] = get_section_text(["Applicability", "Trigger", "Prerequisite"])

        # --- 3. Key Information (Sidebar/Table values) ---
        
        def get_labeled_value(label_patterns):
            for pattern in label_patterns:
                try:
                    # Find element with text "Fee"
                    xpath = f"//*[contains(text(), '{pattern}')]"
                    # We usually want the last occurrence if it's in a sidebar
                    el = page.locator(xpath) 
                    if el.count() > 0:
                        # Logic: The value is usually in the parent container's text
                        # e.g. <div><span>Fee:</span> 500</div>
                        full_text = el.last.locator("xpath=..").inner_text()
                        cleaned = clean_text(full_text.replace(pattern, "").replace(":", ""))
                        if len(cleaned) < 2: # If empty, maybe it's in the next sibling
                             next_sib = el.last.locator("xpath=following-sibling::*[1]")
                             if next_sib.count(): return clean_text(next_sib.inner_text())
                        return cleaned
                except:
                    continue
            return "N/A"

        data["Application Fee"] = get_labeled_value(["Fee", "Payment", "Cost", "Price"])
        data["Validity"] = get_labeled_value(["Validity", "Valid For"])
        data["Average Time taken to get this"] = get_labeled_value(["Time", "Timeline", "SLA", "Duration"])

        # --- 4. NSWS Applicability ---
        apply_btn = page.locator("button:has-text('Apply Now'), a:has-text('Apply Now'), button:has-text('Login to Apply')")
        data["Can be applied through NSWS"] = "Yes" if apply_btn.count() > 0 else "No (Information Only)"

    except Exception as e:
        print(f"      Error extracting detail {url}: {e}")
        for k in ALL_HEADERS:
            if k not in data: data[k] = "Error"
    
    finally:
        # Close the tab
        page.close()
    
    return data

def handle_pagination_and_extraction(page, context, csv_path, headers, extra_fields_map=None):
    """
    Iterates through pages and cards with delays.
    """
    page_num = 1
    while True:
        print(f"   --- Processing Page {page_num} ---")
        
        # Wait for cards to exist
        try:
            page.wait_for_selector(".card, .common-card-container, .approval-license-info", timeout=10000)
        except:
            print("   No cards found on this page.")
            break

        # 1. Identify Cards
        cards = page.locator(".card, .common-card-container, .approval-license-info")
        count = cards.count()
        print(f"   Found {count} approvals.")

        for i in range(count):
            try:
                # Re-locate card to avoid stale element errors
                card = cards.nth(i)
                link_el = card.locator("a").first
                href = link_el.get_attribute("href")
                
                if href:
                    full_url = urljoin(BASE_URL, href)
                    
                    # EXTRACT DETAILS
                    row_data = extract_detail_page(context, full_url)
                    
                    # Fill specifically passed fields (e.g. from Filter Label)
                    if extra_fields_map:
                        for k, v in extra_fields_map.items():
                            row_data[k] = v
                    
                    # Ensure headers match
                    safe_row = {k: row_data.get(k, "N/A") for k in headers}
                    write_to_csv(csv_path, headers, safe_row)

                    # --- SLOW DOWN: BETWEEN CARDS ---
                    # Wait 2-4 seconds before processing the next card
                    wait_time = random.uniform(2.0, 4.0)
                    time.sleep(wait_time)
                    
            except Exception as e:
                print(f"      Skipping card index {i} due to error: {e}")
                continue

        # 2. Handle Pagination
        # Search for Next button
        next_li = page.locator("li.ant-pagination-next")
        
        # Check if disabled
        if next_li.count() == 0:
            break
        
        class_attr = next_li.get_attribute("class") or ""
        if "ant-pagination-disabled" in class_attr:
            print("   Reached last page.")
            break
        
        try:
            next_btn = next_li.locator("a, button").first
            if next_btn.count() > 0:
                print("   Navigating to next page...")
                next_btn.click()
                
                # --- SLOW DOWN: PAGE LOAD ---
                # Wait for new page content to load
                time.sleep(3)
                page_num += 1
            else:
                break
        except Exception as e:
            print(f"   Pagination error: {e}")
            break

def click_filter_and_process(page, context, section_name, csv_path, headers, header_key):
    """
    Handles sidebar filtering for Ministries/Departments.
    """
    print(f"\n=== Starting Phase: {section_name} ===")
    
    # Wait for sidebar
    page.wait_for_selector(".filter-section", state="visible", timeout=15000)
    
    # Locate section (Heuristic: search for text "Ministry" or "Department")
    # This logic finds the header, then gets the container div
    filter_headers = page.locator(".filter-type h3, .filter-type .title, .filter-head")
    target_block = None
    
    for i in range(filter_headers.count()):
        txt = filter_headers.nth(i).inner_text()
        if section_name.lower() in txt.lower():
            target_block = filter_headers.nth(i).locator("xpath=ancestor::div[contains(@class, 'filter-type')]")
            break
    
    # Fallback if text search fails (0=Ministry, 1=Department usually)
    if not target_block:
        idx = 0 if "Ministr" in section_name else 1
        target_block = page.locator(".filter-type").nth(idx)

    # Get Checkboxes
    checkboxes = target_block.locator("label.ant-checkbox-wrapper")
    total_filters = checkboxes.count()
    print(f"Found {total_filters} filters in {section_name}")

    for i in range(total_filters):
        try:
            # Re-locate to prevent stale element
            current_checkbox = checkboxes.nth(i)
            filter_name_raw = current_checkbox.inner_text().strip()
            filter_name = re.sub(r'\(\d+\)$', '', filter_name_raw).strip() # Remove (12) count
            
            print(f"\nApplying Filter [{i+1}/{total_filters}]: {filter_name}")
            
            # Click Filter
            current_checkbox.scroll_into_view_if_needed()
            current_checkbox.click()
            
            # Wait for list update
            time.sleep(3) 
            
            # Extract
            handle_pagination_and_extraction(
                page, context, csv_path, headers, 
                extra_fields_map={header_key: filter_name}
            )
            
            # Uncheck Filter
            current_checkbox.click()
            
            # Wait for reset
            time.sleep(2)
            
        except Exception as e:
            print(f"Error processing filter {i}: {e}")
            page.reload() # Reset state if something breaks
            time.sleep(4)
            # Re-find block
            checkboxes = target_block.locator("label.ant-checkbox-wrapper")

# ==========================================
# MAIN EXECUTION
# ==========================================

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS_MODE)
        context = browser.new_context()
        page = context.new_page()
        
        # Initial Load
        print(f"Navigating to {BASE_URL}")
        page.goto(BASE_URL, timeout=60000)
        time.sleep(3)
        
        # --- PHASE 1: MINISTRIES ---
        try:
            click_filter_and_process(
                page, context, 
                "Ministries", 
                OUTPUT_DIR / "Ministries.csv", 
                MINISTRY_HEADERS, 
                "Ministry Name"
            )
        except Exception as e:
            print(f"Phase 1 Error: {e}")
        
        page.reload()
        time.sleep(4)

        # --- PHASE 2: DEPARTMENTS ---
        try:
            click_filter_and_process(
                page, context, 
                "Departments", 
                OUTPUT_DIR / "Departments.csv", 
                DEPT_HEADERS, 
                "Department Name"
            )
        except Exception as e:
            print(f"Phase 2 Error: {e}")

        page.reload()
        time.sleep(4)

        # --- PHASE 3: ALL (NO FILTERS) ---
        print("\n=== Starting Phase: ALL APPROVALS (No Filter) ===")
        handle_pagination_and_extraction(
            page, context, 
            OUTPUT_DIR / "All_Approvals.csv", 
            ALL_HEADERS
        )

        print("Automation Complete.")
        browser.close()

if __name__ == "__main__":
    main()