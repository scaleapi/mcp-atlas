import type { MLBGame, NBAGame, NFLGame } from '@balldontlie/sdk';

export function formatNBAGame(game: NBAGame): string {
  return `Game ID: ${game.id}\n`
    + `Date: ${game.date}\n`
    + `Season: ${game.season}\n`
    + `Status: ${game.status}\n`
    + `Period: ${game.period}\n`
    + `Time: ${game.time}\n`
    + `Postseason: ${game.postseason}\n`
    + `Score: ${game.home_team.full_name} ${game.home_team_score} - ${game.visitor_team_score} ${game.visitor_team.full_name}\n`
    + `Home Team: ${game.home_team.full_name} (${game.home_team.abbreviation})\n`
    + `Visitor Team: ${game.visitor_team.full_name} (${game.visitor_team.abbreviation})\n`;
}

export function formatMLBGame(game: MLBGame): string {
  // Format inning scores nicely
  const homeInnings = game.home_team_data.inning_scores.map((score, i) => `Inning ${i + 1}: ${score}`).join(', ');
  const awayInnings = game.away_team_data.inning_scores.map((score, i) => `Inning ${i + 1}: ${score}`).join(', ');

  return `Game ID: ${game.id}\n`
    + `Date: ${game.date}\n`
    + `Season: ${game.season}\n`
    + `Postseason: ${game.postseason}\n`
    + `Status: ${game.status}\n`
    + `Venue: ${game.venue}\n`
    + `Attendance: ${game.attendance}\n`
    + `\nMatchup: ${game.home_team_name} vs ${game.away_team_name}\n`
    + `\nHome Team: ${game.home_team.display_name} (${game.home_team.abbreviation})\n`
    + `  League: ${game.home_team.league}\n`
    + `  Division: ${game.home_team.division}\n`
    + `  Runs: ${game.home_team_data.runs}\n`
    + `  Hits: ${game.home_team_data.hits}\n`
    + `  Errors: ${game.home_team_data.errors}\n`
    + `  Inning Scores: ${homeInnings}\n`
    + `\nAway Team: ${game.away_team.display_name} (${game.away_team.abbreviation})\n`
    + `  League: ${game.away_team.league}\n`
    + `  Division: ${game.away_team.division}\n`
    + `  Runs: ${game.away_team_data.runs}\n`
    + `  Hits: ${game.away_team_data.hits}\n`
    + `  Errors: ${game.away_team_data.errors}\n`
    + `  Inning Scores: ${awayInnings}\n`
    + `\nFinal Score: ${game.home_team_name} ${game.home_team_data.runs} - ${game.away_team_data.runs} ${game.away_team_name}`;
}

export function formatNFLGame(game: NFLGame) {
  const winner = game.home_team_score > game.visitor_team_score
    ? game.home_team.full_name
    : game.visitor_team_score > game.home_team_score
      ? game.visitor_team.full_name
      : 'Tie';

  return `Game ID: ${game.id}\n`
    + `Date: ${game.date}\n`
    + `Season: ${game.season}\n`
    + `Week: ${game.week}\n`
    + `Status: ${game.status}\n`
    + `Postseason: ${game.postseason}\n`
    + `Venue: ${game.venue}\n`
    + `Summary: ${game.summary}\n`
    + `\nMatchup: ${game.home_team.full_name} vs ${game.visitor_team.full_name}\n`
    + `\nHome Team: ${game.home_team.full_name} (${game.home_team.abbreviation})\n`
    + `  Location: ${game.home_team.location}\n`
    + `  Conference: ${game.home_team.conference}\n`
    + `  Division: ${game.home_team.division}\n`
    + `  Score: ${game.home_team_score}\n`
    + `\nVisitor Team: ${game.visitor_team.full_name} (${game.visitor_team.abbreviation})\n`
    + `  Location: ${game.visitor_team.location}\n`
    + `  Conference: ${game.visitor_team.conference}\n`
    + `  Division: ${game.visitor_team.division}\n`
    + `  Score: ${game.visitor_team_score}\n`
    + `\nFinal Score: ${game.home_team.full_name} ${game.home_team_score} - ${game.visitor_team_score} ${game.visitor_team.full_name}\n`
    + `Winner: ${winner}`;
}
