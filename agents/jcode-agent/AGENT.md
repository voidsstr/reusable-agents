# Jcode Agent

## Overview
The Jcode Agent is a wrapper for the Jcode harness that allows it to be invoked as part of the reusable-agents framework. This agent provides access to the Jcode Agent API from within the framework's ecosystem. 

## Capabilities
- Execute Jcode Agent prompts and commands
- Leverage the full Jcode harness functionality
- Integrate with the reusable-agents dashboard and workflow

## Usage

### Manual Trigger
The agent can be triggered manually via the dashboard or CLI.

### Chained Trigger  
The agent can also be dispatched by other agents through the framework's dispatch mechanism.

## Configuration
- **Entry Command**: `PYTHONPATH=/home/voidsstr/development/reusable-agents python3 /home/voidsstr/development/reusable-agents/agents/jcode-agent/agent.py`
- **Runnable Modes**: [`"manual"`, `"chained"`]
- **Confirmation Flow**: None (direct execution)

## Input/Output
- The agent accepts prompts and parameters through environment variables
- Results are logged and displayed in the framework's run history

## Dependencies
- Requires Jcode harness to be available in the PATH
- No additional dependencies beyond the Jcode core installation
