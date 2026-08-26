import argparse
import logging
from mock_generator import generate_mock_ticket_data_chunked

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main() -> None:
    """
    티켓 예매 어뷰징 탐지 데이터 파이프라인의 진입점입니다.
    사용자의 CLI 입력을 해석하고, 모의 데이터 생성부터 비식별화까지의 전체 흐름을 제어합니다.
    """
    parser = argparse.ArgumentParser(description="티켓 예매 어뷰징 탐지 파이프라인 컨트롤러")
    
    parser.add_argument('--count', type=int, default=100000, help='생성할 총 데이터 건수 (기본값: 100,000)')
    parser.add_argument('--abuse-ratio', type=float, default=0.05, help='어뷰징(중복) 데이터 비율 (기본값: 0.05)')
    parser.add_argument('--pool-size', type=int, default=20, help='어뷰징 패턴 고정 풀 크기 (기본값: 20)')
    parser.add_argument('--output', type=str, default='mock_ticket_data.csv', help='결과물 CSV 파일 경로 및 이름')

    args = parser.parse_args()

    logger.info(" 티켓 예매 어뷰징 탐지 데이터 파이프라인 구동 시작 ")
    logger.info(f"설정된 목표 건수: {args.count:,}건")
    logger.info(f"설정된 어뷰징 비율: {args.abuse_ratio * 100:.1f}%")
    logger.info(f"설정된 어뷰징 풀 크기: {args.pool_size}개")
    
    # 데이터 생성
    logger.info("\n--- 모의 데이터 생성 파이프라인 구동 ---")
    generate_mock_ticket_data_chunked(
        total_records=args.count, 
        abuse_ratio=args.abuse_ratio,
        output_file=args.output,
        abuse_pool_size=args.pool_size,
        chunk_size=10000
    )
    logger.info(f"데이터 생성 완료: 모의 데이터가 '{args.output}'에 저장되었습니다.\n")
    
    # TODO: Jaro-Winkler 기반 배송지 주소 문자열 정규화 모듈 연동 예정
    
    # TODO: Argon2id 기반 데이터 단방향 암호화 모듈 연동 예정

    logger.info(" 파이프라인 처리가 성공적으로 종료되었습니다. ")

if __name__ == "__main__":
    main()