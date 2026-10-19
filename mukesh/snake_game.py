# snake_game.py – a simple 2‑D Snake game using pygame
# -------------------------------------------------------
# This script creates a window with a grid where a snake moves,
# eats food and grows. The game ends when the snake collides
# with the wall or itself.
# -------------------------------------------------------

import pygame
import random
import sys

# ----- Configuration -------------------------------------------------
WINDOW_WIDTH = 600
WINDOW_HEIGHT = 600
CELL_SIZE = 20  # size of each grid cell in pixels
GRID_WIDTH = WINDOW_WIDTH // CELL_SIZE
GRID_HEIGHT = WINDOW_HEIGHT // CELL_SIZE

# Colors (R, G, B)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREEN = (0, 255, 0)
RED = (255, 0, 0)

# Speed (frames per second)
FPS = 10
# ---------------------------------------------------------------------

pygame.init()
screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
pygame.display.set_caption('Snake')
clock = pygame.time.Clock()
font = pygame.font.SysFont('arial', 25)

def draw_grid():
    for x in range(0, WINDOW_WIDTH, CELL_SIZE):
        pygame.draw.line(screen, WHITE, (x, 0), (x, WINDOW_HEIGHT))
    for y in range(0, WINDOW_HEIGHT, CELL_SIZE):
        pygame.draw.line(screen, WHITE, (0, y), (WINDOW_WIDTH, y))

def random_food_position(snake_body):
    while True:
        pos = (random.randint(0, GRID_WIDTH - 1), random.randint(0, GRID_HEIGHT - 1))
        if pos not in snake_body:
            return pos

def draw_rect(position, color):
    rect = pygame.Rect(position[0] * CELL_SIZE, position[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
    pygame.draw.rect(screen, color, rect)

def show_message(text, color, y_offset=0):
    msg = font.render(text, True, color)
    rect = msg.get_rect(center=(WINDOW_WIDTH // 2, WINDOW_HEIGHT // 2 + y_offset))
    screen.blit(msg, rect)

def main():
    # Initial snake: three blocks moving to the right
    snake = [(GRID_WIDTH // 2, GRID_HEIGHT // 2),
             (GRID_WIDTH // 2 - 1, GRID_HEIGHT // 2),
             (GRID_WIDTH // 2 - 2, GRID_HEIGHT // 2)]
    direction = (1, 0)  # (dx, dy)
    food = random_food_position(snake)
    score = 0
    game_over = False

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP and direction != (0, 1):
                    direction = (0, -1)
                elif event.key == pygame.K_DOWN and direction != (0, -1):
                    direction = (0, 1)
                elif event.key == pygame.K_LEFT and direction != (1, 0):
                    direction = (-1, 0)
                elif event.key == pygame.K_RIGHT and direction != (-1, 0):
                    direction = (1, 0)
                elif event.key == pygame.K_r and game_over:
                    # Restart the game
                    main()

        if not game_over:
            # Move snake
            new_head = (snake[0][0] + direction[0], snake[0][1] + direction[1])
            # Check wall collision
            if (new_head[0] < 0 or new_head[0] >= GRID_WIDTH or
                new_head[1] < 0 or new_head[1] >= GRID_HEIGHT):
                game_over = True
            # Check self collision
            elif new_head in snake:
                game_over = True
            else:
                snake.insert(0, new_head)
                # Check food consumption
                if new_head == food:
                    score += 1
                    food = random_food_position(snake)
                else:
                    snake.pop()  # remove tail

        # ----- Drawing -------------------------------------------------
        screen.fill(BLACK)
        draw_grid()
        # Draw food
        draw_rect(food, RED)
        # Draw snake
        for segment in snake:
            draw_rect(segment, GREEN)
        # Score display
        score_surf = font.render(f'Score: {score}', True, WHITE)
        screen.blit(score_surf, (10, 10))
        # Game over overlay
        if game_over:
            show_message('Game Over! Press R to Restart', RED)
        pygame.display.flip()
        clock.tick(FPS)

if __name__ == '__main__':
    main()
