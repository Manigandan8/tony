import pygame
import random
import sys

# Settings
WIDTH, HEIGHT = 600, 400
CELL_SIZE = 20
GRID_WIDTH = WIDTH // CELL_SIZE
GRID_HEIGHT = HEIGHT // CELL_SIZE
FPS = 10

# Colors
BLACK = (0, 0, 0)
GREEN = (0, 255, 0)
RED = (255, 0, 0)
WHITE = (255, 255, 255)

# Directions
UP = (0, -1)
DOWN = (0, 1)
LEFT = (-1, 0)
RIGHT = (1, 0)

def draw_rect(surface, color, pos):
    rect = pygame.Rect(pos[0] * CELL_SIZE, pos[1] * CELL_SIZE, CELL_SIZE, CELL_SIZE)
    pygame.draw.rect(surface, color, rect)

def random_cell():
    return (random.randint(0, GRID_WIDTH - 1), random.randint(0, GRID_HEIGHT - 1))

def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Snake 2D")
    clock = pygame.time.Clock()

    snake = [(GRID_WIDTH // 2, GRID_HEIGHT // 2)]
    direction = random.choice([UP, DOWN, LEFT, RIGHT])
    food = random_cell()
    while food in snake:
        food = random_cell()

    score = 0
    font = pygame.font.SysFont(None, 24)
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP and direction != DOWN:
                    direction = UP
                elif event.key == pygame.K_DOWN and direction != UP:
                    direction = DOWN
                elif event.key == pygame.K_LEFT and direction != RIGHT:
                    direction = LEFT
                elif event.key == pygame.K_RIGHT and direction != LEFT:
                    direction = RIGHT
        # Move snake
        head = (snake[0][0] + direction[0], snake[0][1] + direction[1])
        head = (head[0] % GRID_WIDTH, head[1] % GRID_HEIGHT)
        if head in snake:
            running = False
            continue
        snake.insert(0, head)
        if head == food:
            score += 1
            food = random_cell()
            while food in snake:
                food = random_cell()
        else:
            snake.pop()
        # Draw
        screen.fill(BLACK)
        draw_rect(screen, RED, food)
        for segment in snake:
            draw_rect(screen, GREEN, segment)
        score_surf = font.render(f"Score: {score}", True, WHITE)
        screen.blit(score_surf, (5, 5))
        pygame.display.flip()
        clock.tick(FPS)
    # Game over screen
    screen.fill(BLACK)
    msg = font.render(f"Game Over! Final Score: {score}", True, WHITE)
    rect = msg.get_rect(center=(WIDTH // 2, HEIGHT // 2))
    screen.blit(msg, rect)
    pygame.display.flip()
    pygame.time.wait(3000)
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
