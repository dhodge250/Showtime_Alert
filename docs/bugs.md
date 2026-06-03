## Login
- The login card should not stretch across the entire screen, and instead should be an independant-sized card that sits in the middle of the screen, leaving empty space around it when viewed on larger screens
## Dashboard
- I have multiple alerts setup for multiple movies (The Manalorian and Grogu, and Project Hail Mary), but yet the movies tracked card only says 1. This should say 2
- In the "Available Showtimes" card, the "Filter by movie..." and "Filter by theater..." boxes should be a dropdown list and allow for multiple selections
- The "Available Showtimes" card should limit the number of showtimes displayed (a configurable value in the Admin settings), default to 15. If more than the configured amount, there should be page buttons at the bottom of the list to see the other options
- The "Available Showtimes" card and "Active Alerts" card lengths should be independant from one another. If one is longer than the other it shouldn't extend the shorter card.
- Similar to "Available Showtimes", the "Active Alerts" card should allow for filtering of alerts with the same options that the showtimes filters use, and should also have a limit to the number of alerts displayed.
## Theaters
- The layout should be shifted a bit to make room for all of the filters/search bar options. Move the map down so the top of the map aligns with the top of the theater cards, and the filter/search options should sit on top of both
- The user's "home" location is not displayed on the map
## Alerts
- Similar to the bug on the "Dashboard" page, the "Create New Alert" and "Your Alerts" card lenghts should be independant from one another.
- For one of my alerts, I set the "Notification limit" to something like 5 or 10, but when an alert was sent out only 1 alert was sent but the alert itself remains open with the "All Sent" status. This should work where the notification limit controls how many notifications are sent out for that alert once the showtimes are found.
### Alert Detail
- Within the "Alert Information" card, the "Notification limit" number that was configured for the alert should be displayed here, and should also show the count or remaining notifications for that alert
- In the "Matching Showtimes" card, the Date & Time values are formatted like "Fri, May 29, 2026 &ndash; 12:30 PM" and instead need to be formatted like "Jun 04, 2026 09:30 PM"
## Profile
- The "Your Location" card should be aligned in the middle between "Account Info" and "Preferences", instead of below "Account Info". Of course, these should shift location depending on the screen size.
## Admin
### Settings
- The "Global Defaults" card should be moved somewhere to the right of the notifications cards. Most of these cards don't look very well organized, and their sizes seem off. Maybe the notifications should all be in one section/card, external integrations should be in their own card (currently only TMDB), and scheduler should be updated to look better (it currently looks very large for the info being configured in it)
  - The new notification limit default config will need to be considered here as well.