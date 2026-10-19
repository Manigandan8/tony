import pygame
import sys
import random

# Initialize pygame
pygame.init()

# Screen dimensions
WIDTH, HEIGHT = 600, 400
CELL_SIZE = 20
GRID_WIDTH = WIDTH // CELL_SIZE
GRID_HEIGHT = HEIGHT // CELL_SIZE

# Colors
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (200, 0, 0)
GREEN = (0, 200, 0)

# Set up display
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption('Snake Game')
clock = pygame.time.Clock()

font = pygame.font.SysFont('Arial', 25)

def draw_grid():
    for x in range(0, WIDTH, CELL_SIZE):
        pygame.draw.line(screen, BLACK, (x, 0), (x, HEIGHT))
    for y in range(0, HEIGHT, CELL_SIZE):
        pygame.draw.line(screen, BLACK, (0, y), (WIDTH, y))

def random_food_position(snake):
    while True:
        pos = (random.randint(0, GRID_WIDTH - 1), random.randint(0, GRID_HEIGHT - 1))
        if pos not in snake:
            return pos

def main():
    snake = [(GRID_WIDTH // 2, GRID_HEIGHT // 2)]
    direction = (0, -1)  # start moving up
    food = random_food_position(snake)
    score = 0
    speed = 10

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

        # Move snake
        new_head = (snake[0][0] + direction[0], snake[0][1] + direction[1])
        # Check collisions with walls
        if (new_head[0] < 0 or new_head[0] >= GRID_WIDTH or
                new_head[1] < 0 or new_head[1] >= GRID_HEIGHT):
            break  # Game over
        # Check collisions with self
        if new_head in snake:
            break  # Game over
        snake.insert(0, new_head)
        # Check if food eaten
        if new_head == food:
            score += 1
            food = random_food_position(snake)
            speed = min(20, speed + 0.5)  # increase speed slightly
        else:
            snake.pop()

        # Draw everything
        screen.fill(WHITE)
        draw_grid()
        # Draw food
        food_rect = pygame.Rect(food[0] * CELL_SIZE, food[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
        pygame.draw.rect(screen, RED, food_rect)
        # Draw snake
        for segment in snake:
            seg_rect = pygame.Rect(segment[0] * CELL_SIZE, segment[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(screen, GREEN, seg_rect)
        # Draw score
        score_surf = font.render(f'Score: {score}', True, BLACK)
        screen.blit(score_surf, (5, 5))
        pygame.display.flip()
        clock.tick(speed)

    # Game over screen
    screen.fill(WHITE)
    game_over_surf = font.render('Game Over! Press any key to exit.', True, RED)
    screen.blit(game_over_surf, (WIDTH // 2 - game_over_surf.get_width() // 2, HEIGHT // 2))
    pygame.display.flip()
    # Wait for key press
    waiting = True
    while waiting:
        for event in pygame.event.get():
            if event.type == pygame.KEYDOWN or event.type == pygame.QUIT:
                waiting = False
    pygame.quit()
    sys.exit()

if __name__ == '__main__':
    main()
