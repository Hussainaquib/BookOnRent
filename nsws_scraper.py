import time
import csv
import re
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# Configuration
BASE_URL = "https://www.nsws.gov.in/portal/approvalsandregistrations"
OUTPUT_DIR = Path("nsws_data_output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------
# CSV HEADERS
# --------------------------
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

# --------------------------
# HELPER FUNCTIONS
# --------------------------

def clean_text(text):
    if not text:
        return "N/A"
    # Remove HTML tags if any linger, collapse whitespace
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def write_to_csv(filepath, headers, row_data):
    file_exists = filepath.exists()
    with open(filepath, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_data)

def extract_detail_page(context, url):
    """
    Opens the detail URL in a new page, scrapes the specific fields, and closes it.
    Returns a dictionary of extracted data.
    """
    page = context.new_page()
    data = {}
    
    try:
        # Go to URL with robust wait
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector("h1, h2, .approval-title", timeout=10000)
        except:
            pass

        # --- 1. Basic Info ---
        data["Approval Link"] = url
        
        # Name
        h1 = page.locator("h1").first
        data["Approval Name"] = clean_text(h1.inner_text()) if h1.count() else "N/A"

        # Ministry / Department (Usually in breadcrumb or top banner)
        # We try to grab generic text from banner if specific classes aren't stable
        banner_text = page.locator(".banner-content, .approval-banner").inner_text() if page.locator(".banner-content, .approval-banner").count() else ""
        data["Ministry Name"] = "N/A"
        data["Department Name"] = "N/A"
        
        # Heuristic extraction for Ministry/Dept from banner text lines
        if banner_text:
            lines = [l.strip() for l in banner_text.split('\n') if l.strip()]
            for line in lines:
                if "Ministry" in line:
                    data["Ministry Name"] = line
                if "Department" in line:
                    data["Department Name"] = line

        # --- 2. Content Sections (About, Who, Docs) ---
        # NSWS usually uses standard headers. We look for the header, then get the content immediately following it.
        
        def get_section_text(header_keywords):
            # Try to find a header containing the keyword
            for keyword in header_keywords:
                # Locator for H2, H3, H4, or strong containing text
                xpath = f"//*[self::h2 or self::h3 or self::h4 or self::strong or self::b][contains(text(), '{keyword}')]"
                header = page.locator(xpath).first
                if header.count() > 0:
                    # Get the next sibling div or p
                    # We might need to traverse up if the header is inside a wrapper
                    try:
                        content = page.locator(xpath + "/following::div[1]").first
                        return clean_text(content.inner_text())
                    except:
                        return "N/A"
            return "N/A"

        data["About this approval"] = get_section_text(["About", "Brief", "Description"])
        data["Who can apply"] = get_section_text(["Who can", "Eligibility"])
        data["Documents required"] = get_section_text(["Documents", "Enclosures", "Attachment"])
        data["Approval applicability/trigger"] = get_section_text(["Applicability", "Trigger"])

        # --- 3. Key Information (Sidebars/Tables) ---
        # Fee, Validity, Time are often in labeled value pairs
        
        def get_labeled_value(label_patterns):
            for pattern in label_patterns:
                try:
                    # Look for a label, then find the value nearby
                    # Strategy: Find element with text "Fee", check next sibling
                    xpath = f"//*[contains(text(), '{pattern}')]"
                    el = page.locator(xpath).last # often last instance in sidebar
                    if el.count():
                        # Assumption: Value is in the next element or parent's next element
                        # Try grabbing text of the parent 
                        full_text = el.locator("xpath=..").inner_text()
                        return clean_text(full_text.replace(pattern, ""))
                except:
                    continue
            return "N/A"

        data["Application Fee"] = get_labeled_value(["Fee", "Payment", "Cost"])
        data["Validity"] = get_labeled_value(["Validity", "Valid For"])
        data["Average Time taken to get this"] = get_labeled_value(["Time", "Timeline", "SLA"])

        # --- 4. NSWS Applicability ---
        # Check for "Apply Now" button or specific text
        apply_btn = page.locator("button:has-text('Apply Now'), a:has-text('Apply Now'), button:has-text('Login to Apply')")
        if apply_btn.count() > 0:
            data["Can be applied through NSWS"] = "Yes"
        else:
            data["Can be applied through NSWS"] = "No (Information Only)"

    except Exception as e:
        print(f"Error extracting detail {url}: {e}")
        # Fill defaults
        for k in ALL_HEADERS:
            if k not in data: data[k] = "Error"
    
    finally:
        page.close()
    
    return data

def handle_pagination_and_extraction(page, context, csv_path, headers, extra_fields_map=None):
    """
    Iterates through pages and cards.
    extra_fields_map: dict to force specific values (e.g. {'Ministry Name': 'Ministry of X'})
    """
    while True:
        # 1. Identify Cards
        # Using a broad selector to catch all types of cards
        cards = page.locator(".card, .common-card-container, .approval-license-info")
        count = cards.count()
        print(f"   Found {count} approvals on this page.")

        for i in range(count):
            try:
                card = cards.nth(i)
                # Find the link
                link_el = card.locator("a").first
                href = link_el.get_attribute("href")
                
                if href:
                    full_url = urljoin(BASE_URL, href)
                    print(f"      Processing: {full_url} ...")
                    
                    # EXTRACT DETAILS
                    row_data = extract_detail_page(context, full_url)
                    
                    # Fill specifically passed fields (e.g. from the Filter Label)
                    if extra_fields_map:
                        for k, v in extra_fields_map.items():
                            row_data[k] = v
                    
                    # Ensure all CSV headers exist in row_data
                    safe_row = {k: row_data.get(k, "N/A") for k in headers}
                    
                    write_to_csv(csv_path, headers, safe_row)
                    
            except Exception as e:
                print(f"      Skipping card due to error: {e}")
                continue

        # 2. Handle Pagination (Next Button)
        # Based on divPagination.txt: <li class="ant-pagination-next" ...>
        next_li = page.locator("li.ant-pagination-next")
        
        if next_li.count() == 0 or "ant-pagination-disabled" in (next_li.get_attribute("class") or ""):
            print("   No next page or disabled.")
            break
        
        try:
            # Click the anchor tag inside the li
            next_btn = next_li.locator("a, button").first
            if next_btn.count() > 0:
                print("   Navigate to next page...")
                with page.expect_response(lambda response: response.status == 200, timeout=10000):
                    next_btn.click()
                time.sleep(2) # Allow React to render new cards
            else:
                break
        except Exception as e:
            print(f"   Pagination error: {e}")
            break

def click_filter_and_process(page, context, section_name, csv_path, headers, header_key_for_filter_name):
    """
    Generic function to handle Ministry or Department sidebar filtering
    header_key_for_filter_name: 'Ministry Name' or 'Department Name'
    """
    print(f"\n=== Starting Phase: {section_name} ===")
    
    # 1. Expand/Locate the Section
    # We look for the "Ministry" or "Department" headers in the filter sidebar
    # Note: Selectors might need tuning based on exact sidebar HTML, usually .filter-type
    
    # Wait for filters to load
    page.wait_for_selector(".filter-section", state="visible")
    
    # Find the block corresponding to section_name
    # Assuming order: Ministry is usually first, Dept second, or identified by text
    # Strategy: Find all filter headers, look for text
    filter_headers = page.locator(".filter-type h3, .filter-type .title, .filter-head") 
    
    target_block = None
    count = filter_headers.count()
    for i in range(count):
        txt = filter_headers.nth(i).inner_text()
        if section_name.lower() in txt.lower():
            # Get the parent container
            target_block = filter_headers.nth(i).locator("xpath=ancestor::div[contains(@class, 'filter-type')]")
            break
    
    if not target_block:
        # Fallback: Just assume 1st is Ministry, 2nd is Dept if names fail
        fallback_idx = 0 if "Ministry" in section_name else 1
        target_block = page.locator(".filter-type").nth(fallback_idx)

    # Get all checkboxes within this block
    checkboxes = target_block.locator("label.ant-checkbox-wrapper")
    total_filters = checkboxes.count()
    print(f"Found {total_filters} filters in {section_name}")

    for i in range(total_filters):
        # Refresh reference in loop to avoid stale elements
        try:
            current_checkbox = checkboxes.nth(i)
            filter_name_raw = current_checkbox.inner_text().strip()
            # Clean name (remove counts like (12))
            filter_name = re.sub(r'\(\d+\)$', '', filter_name_raw).strip()
            
            print(f"\nApplying Filter [{i+1}/{total_filters}]: {filter_name}")
            
            # Click
            current_checkbox.scroll_into_view_if_needed()
            current_checkbox.click()
            
            # Wait for results
            time.sleep(2) 
            
            # Process results
            # We enforce the Ministry/Dept name in the output to match the filter
            handle_pagination_and_extraction(
                page, context, csv_path, headers, 
                extra_fields_map={header_key_for_filter_name: filter_name}
            )
            
            # Uncheck to reset for next iteration
            current_checkbox.click()
            time.sleep(1)
            
        except Exception as e:
            print(f"Error processing filter {i}: {e}")
            # Try to recover: uncheck all or reload
            page.reload()
            time.sleep(3)
            # Re-locate block
            target_block = page.locator(".filter-type").nth(0 if "Ministry" in section_name else 1)
            checkboxes = target_block.locator("label.ant-checkbox-wrapper")


# --------------------------
# MAIN EXECUTION
# --------------------------

def main():
    with sync_playwright() as p:
        # Launch options
        browser = p.chromium.launch(headless=False) # Headless=False to see it working
        context = browser.new_context()
        page = context.new_page()
        
        print(f"Navigating to {BASE_URL}")
        page.goto(BASE_URL, timeout=60000)
        
        # --- PHASE 1: MINISTRIES ---
        click_filter_and_process(
            page, context, 
            "Ministries", 
            OUTPUT_DIR / "Ministries.csv", 
            MINISTRY_HEADERS, 
            "Ministry Name"
        )
        
        # Ensure clean state (reload)
        page.reload()
        time.sleep(3)

        # --- PHASE 2: DEPARTMENTS ---
        click_filter_and_process(
            page, context, 
            "Departments", 
            OUTPUT_DIR / "Departments.csv", 
            DEPT_HEADERS, 
            "Department Name"
        )

        # Ensure clean state
        page.reload()
        time.sleep(3)

        # --- PHASE 3: ALL (NO FILTERS) ---
        print("\n=== Starting Phase: ALL APPROVALS (No Filter) ===")
        # Just process the default list
        handle_pagination_and_extraction(
            page, context, 
            OUTPUT_DIR / "All_Approvals.csv", 
            ALL_HEADERS
        )

        print("Automation Complete.")
        browser.close()

if __name__ == "__main__":
    main()