# Makefile for "This is no cave" Influencer Outreach System
# Game marketing automation toolkit

.PHONY: help install setup discover generate send clean test

# Default target
help:
	@echo "════════════════════════════════════════════════════════════"
	@echo "  This is no cave - Influencer Outreach System"
	@echo "════════════════════════════════════════════════════════════"
	@echo ""
	@echo "Setup Commands:"
	@echo "  make install          Install Python dependencies"
	@echo "  make setup            Complete first-time setup"
	@echo ""
	@echo "Discovery Commands:"
	@echo "  make discover         Find influencers (YouTube + Twitch)"
	@echo "  make discover-test    Quick test with limited results"
	@echo ""
	@echo "Email Generation Commands:"
	@echo "  make generate         Generate Gmail drafts for top 50"
	@echo "  make generate-test    Generate test drafts (10 influencers)"
	@echo "  make generate-txt     Generate text file instead of Gmail drafts"
	@echo ""
	@echo "Utility Commands:"
	@echo "  make stats            Show campaign statistics"
	@echo "  make followup         Generate follow-up emails"
	@echo "  make clean            Clean up generated files"
	@echo "  make backup           Backup all data files"
	@echo ""
	@echo "Required Files:"
	@echo "  - steam_keys.txt      Your Steam keys (one per line)"
	@echo "  - credentials.json    Gmail API credentials (see README)"
	@echo ""

# Installation
install:
	@echo "📦 Installing Python dependencies..."
	poetry install --no-root
	@echo "✓ Dependencies installed successfully!"

# First-time setup
setup: install
	@echo ""
	@echo "🔧 Setting up your environment..."
	@echo ""
	@echo "Please ensure you have:"
	@echo "1. YouTube API key"
	@echo "2. Twitch Client ID and Secret"
	@echo "3. Gmail API credentials.json"
	@echo ""
	@echo "Edit the configuration in both scripts:"
	@echo "  - influencer_parser.py (lines 12-15)"
	@echo "  - gmail_draft_generator.py (lines 12-15)"
	@echo ""
	@read -p "Press Enter when ready to continue..."
	@echo ""
	@echo "✓ Setup complete! Run 'make discover' to find influencers."

# Discovery: Find influencers
discover:
	@echo "🔍 Starting influencer discovery..."
	@echo "This will search YouTube and Twitch for platformer content creators."
	@echo ""
	poetry run python influencer_parser.py
	@echo ""
	@echo "✓ Discovery complete!"
	@echo "Results saved to:"
	@echo "  - influencers_with_contacts.csv"
	@echo "  - influencers_priority_top50.csv (sorted by response likelihood)"
	@echo "  - influencers_backup.json"

# Quick test discovery (fewer API calls)
discover-test:
	@echo "🔍 Running quick discovery test..."
	@echo "Note: Edit MAX_RESULTS in influencer_parser.py to limit API calls"
	poetry run python influencer_parser.py
	@echo ""
	@echo "✓ Test discovery complete!"

# Generate Gmail drafts for top 50 influencers
generate:
	@echo "📧 Generating Gmail drafts for top 50 influencers..."
	@echo ""
	@if [ ! -f "steam_keys.txt" ]; then \
		echo "❌ ERROR: steam_keys.txt not found!"; \
		echo "Please create this file with your Steam keys (one per line)"; \
		exit 1; \
	fi
	@if [ ! -f "influencers_priority_top50.csv" ]; then \
		echo "❌ ERROR: influencers_priority_top50.csv not found!"; \
		echo "Run 'make discover' first to find influencers."; \
		exit 1; \
	fi
	poetry run python gmail_draft_generator.py --csv influencers_priority_top50.csv --keys steam_keys.txt
	@echo ""
	@echo "✓ Drafts created! Check your Gmail drafts folder."

# Generate test drafts (only 10)
generate-test:
	@echo "📧 Generating 10 test drafts..."
	@if [ ! -f "steam_keys.txt" ]; then \
		echo "❌ ERROR: steam_keys.txt not found!"; \
		echo "Creating sample file..."; \
		echo "XXXXX-XXXXX-XXXXX" > steam_keys.txt; \
		echo "YYYYY-YYYYY-YYYYY" >> steam_keys.txt; \
		echo "ZZZZZ-ZZZZZ-ZZZZZ" >> steam_keys.txt; \
		echo "⚠️  Sample keys created. Replace with real keys!"; \
	fi
	poetry run python gmail_draft_generator.py --csv influencers_priority_top50.csv --keys steam_keys.txt --max 10
	@echo ""
	@echo "✓ Test drafts created!"

# Generate text file instead of Gmail drafts
generate-txt:
	@echo "📄 Generating email drafts to text file..."
	poetry run python gmail_draft_generator.py --csv influencers_priority_top50.csv --keys steam_keys.txt --no-gmail
	@echo ""
	@echo "✓ Drafts saved to email_drafts.txt"

# Generate follow-up emails
followup:
	@echo "🔄 Generating follow-up emails for non-responders..."
	poetry run python gmail_draft_generator.py --followup
	@echo ""
	@echo "✓ Follow-up drafts created!"

# Show campaign statistics
stats:
	@echo "📊 Campaign Statistics"
	@echo "════════════════════════════════════════════════════════════"
	@if [ -f "influencers_with_contacts.csv" ]; then \
		echo "Total influencers found: $$(tail -n +2 influencers_with_contacts.csv | wc -l)"; \
	fi
	@if [ -f "influencers_priority_top50.csv" ]; then \
		echo "Priority influencers: $$(tail -n +2 influencers_priority_top50.csv | wc -l)"; \
	fi
	@if [ -f "key_assignments.json" ]; then \
		echo "Keys assigned: $$(grep -o '"key":' key_assignments.json | wc -l)"; \
		echo "Keys sent: $$(grep -o '"sent": true' key_assignments.json | wc -l)"; \
	fi
	@if [ -f "steam_keys.txt" ]; then \
		echo "Keys available: $$(wc -l < steam_keys.txt)"; \
	fi
	@echo ""

# Clean generated files (keeps configuration)
clean:
	@echo "🧹 Cleaning up generated files..."
	@read -p "This will delete all generated CSVs and JSON files. Continue? (y/N) " confirm; \
	if [ "$$confirm" = "y" ] || [ "$$confirm" = "Y" ]; then \
		rm -f influencers_with_contacts.csv; \
		rm -f influencers_priority_top50.csv; \
		rm -f influencers_backup.json; \
		rm -f email_drafts.txt; \
		rm -f email_tracking.json; \
		echo "✓ Cleanup complete!"; \
	else \
		echo "Cancelled."; \
	fi

# Backup all data files
backup:
	@echo "💾 Creating backup..."
	@mkdir -p backups
	@BACKUP_DIR="backups/backup_$$(date +%Y%m%d_%H%M%S)"; \
	mkdir -p $$BACKUP_DIR; \
	cp -f influencers_*.csv $$BACKUP_DIR/ 2>/dev/null || true; \
	cp -f influencers_backup.json $$BACKUP_DIR/ 2>/dev/null || true; \
	cp -f key_assignments.json $$BACKUP_DIR/ 2>/dev/null || true; \
	cp -f email_tracking.json $$BACKUP_DIR/ 2>/dev/null || true; \
	echo "✓ Backup created in $$BACKUP_DIR"

# Full workflow (discover + generate)
all: discover generate
	@echo ""
	@echo "✓ Complete workflow finished!"
	@echo "Check your Gmail drafts and review before sending."

# Test the complete workflow
test: discover-test generate-test
	@echo ""
	@echo "✓ Test workflow complete!"
	@echo "Review the test drafts in Gmail."