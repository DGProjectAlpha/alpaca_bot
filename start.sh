#!/bin/bash
# AlpacaBot Quick Start

echo "🤖 AlpacaBot Setup"
echo "===================="

# Check for .env
if [ ! -f .env ]; then
    echo "No .env file found. Creating from template..."
    cp .env.example .env
    echo ""
    echo "⚠️  EDIT .env WITH YOUR ALPACA API KEYS BEFORE RUNNING!"
    echo "   Get keys from: https://app.alpaca.markets/paper/dashboard/overview"
    echo ""
    exit 1
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt -q

echo ""
echo "Starting AlpacaBot..."
echo ""
python bot.py
