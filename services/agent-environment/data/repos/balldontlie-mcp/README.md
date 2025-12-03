# Balldontlie MCP Server

[![smithery badge](https://smithery.ai/badge/@mikechao/balldontlie-mcp)](https://smithery.ai/server/@mikechao/balldontlie-mcp)

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/mikechao-balldontlie-mcp-badge.png)](https://mseep.ai/app/mikechao-balldontlie-mcp)

<a href="https://glama.ai/mcp/servers/@mikechao/balldontlie-mcp">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@mikechao/balldontlie-mcp/badge" alt="balldontlie-mcp MCP server" />
</a>

An MCP Server implementation that integrates the [Balldontlie API](https://www.balldontlie.io/), to provide information about players, teams and games for the NBA, NFL and MLB.

## Tools

- **get_teams**

  - Get a list of teams for the NBA, NFL or MLB
  - Inputs:
    - `league` (enum ['NBA', 'NFL', 'MLB']): The sports league to get teams for

- **get_players**

  - Gets a list of players for the NBA, NFL or MLB
  - Inputs:
    - `league` (enum ['NBA', 'NFL', 'MLB']): The sports league to get players for
    - `firstName` (string, optional): The first name of the player to search for
    - `lastName` (string, optional): The last name of the player to search for
    - `cursor` (number, optional): Cursor for pagination

- **get_games**

  - Gets the list of games for the NBA, NFL or MLB
  - Inputs:
    - `league` (enum ['NBA', 'NFL', 'MLB']): The sports league to get games for
    - `dates` (string[], optional): Get games for specific dates, format: YYYY-MM-DD
    - `teamIds` (string[], optional): Get games for specific games
    - `cursor` (number, optional): Cursor for pagination

- **get_game**

  - Get a specific game from one of the following leagues NBA, MLB, NFL
  - Inputs:
    - `league` (enum ['NBA', 'NFL', 'MLB']): The sports league to get the game for
    - `gameId` (number): The id of the game from the get_games tool

## Prompts

- **schedule_generator**

Given a league (NBA, MLB, NFL), a starting date and ending date generates an interactive schedule in Claude Desktop.

![claude desktop example](https://mikechao.github.io/images/schedule_geneartor_prompt.webp)

## Configuration

### Getting an API Key

1. Sign up for account at [Balldontlie.io](https://www.balldontlie.io/)
2. The free plan is enough for this MCP Server

### Installing via Smithery

To install balldontlie-mcp for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@mikechao/balldontlie-mcp):

```bash
npx -y @smithery/cli install @mikechao/balldontlie-mcp --client claude
```

### Usage with Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcp-servers": {
    "balldontlie": {
      "command": "npx",
      "args": [
        "-y",
        "balldontlie-mcp"
      ],
      "env": {
        "BALLDONTLIE_API_KEY": "YOUR API KEY HERE"
      }
    }
  }
}
```

### Usage with LibreChat

```yaml
mcpServers:
  balldontlie:
    command: sh
    args:
      - -c
      - BALLDONTLIE_API_KEY=your-api-key-here npx -y balldontlie-mcp
```

## License

This MCP server is licensed under the MIT License. This means you are free to use, modify, and distribute the software, subject to the terms and conditions of the MIT License. For more details, please see the LICENSE file in the project repository.

## Disclaimer

This library is not officially associated with balldontlie.io. It is a third-party implementation of the balldontlie api with a MCP Server.
